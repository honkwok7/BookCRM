# Booking Engine

"Booking" is the model name; the UI and docs call it an **appointment**.

## One entry point

Every change to an appointment goes through `bookings/services.py` (the booking service). A
source scan in `tests/test_booking_service.py` fails CI if any other module creates bookings or
writes their `status`, `start_datetime` or `end_datetime`.

| Function | What it does |
|---|---|
| `create_booking(...)` | See below |
| `cancel_booking(booking, actor, reason, enforce_deadline)` | Locks the row, refuses appointments that are already finished, records who cancelled and why, and queues the cancellation email. `enforce_deadline` (customers acting on their own appointment) applies the service's `cancellation_deadline_hours`; the team isn't held to it |
| `change_booking_status(booking, new_status, actor, note)` | Validates the transition (below), writes status history and an audit entry. Cancelling is delegated to `cancel_booking` |
| `reschedule_booking(booking, new_start, actor, public)` | **One transaction**: closes the old appointment as cancelled ("Rescheduled") and creates a new one (same provider, location, source and customer) linked through `rescheduled_from`. The new time gets every check a new booking gets; the old appointment no longer blocks it. If it is refused, nothing changes. `public`: a customer is moving their own appointment, so the online rules and the cancellation deadline apply |
| `record_past_booking(...)` | Imports an appointment that already happened (demo history, future CSV import): the time must be in the past and the status final (completed, no-show, cancelled), so it never occupies a calendar |

`create_booking` does, in order:

1. Tenant consistency: service, staff, location and customer belong to the organization.
2. Idempotency: a repeated `idempotency_key` returns the booking made the first time (the same
   key with a different service, staff or time is 409 `idempotency_key_reused`). Keys are
   unique per organization; the API accepts `idempotency_key` or an `Idempotency-Key` header.
3. The location: the given one (active, and booking-enabled for the public) or the default.
4. Locks the provider's calendar (`lock_staff`), then runs `AvailabilityService.validate_slot`
   against committed data, for **every** caller: working hours at that location, opening
   hours, closures, holidays, time off, other appointments with buffers, the daily limit, and
   that the provider offers the service there. `public=True` (customers) adds the minimum
   notice and booking window; the team is only kept out of the past.
5. The plan's monthly booking limit (bookings created this calendar month, in the
   organization's time zone): 409 `plan_limit`.
6. Finds or creates the CRM customer (their CRM source follows the booking source).
7. Uses the provider's own duration and price for the service (their offering), and
   snapshots price, duration and the service's buffers on the booking.
8. Records `location`, `source` and `created_by`, writes status history, the activity log,
   audit and the customer's timeline, and queues the confirmation email **after commit**.

`Booking.source` records where the booking came from:

| Source | Set by |
|---|---|
| `public_booking` | The public booking page |
| `customer_portal` | A customer booking through the API (the portal, M5.4) |
| `api` | A team member booking through the API |
| `reception`, `staff` | The reception and provider screens (M4.5); `seed_demo` uses `reception` |
| `ai_agent`, `admin` | Reserved for AI agents (M10) and platform admin tools |
| `import` | `record_past_booking` |

The API exposes these only as explicit actions (`cancel`, `reschedule`, `update_status`).
There is **no generic update or delete** on appointments, so every client (web, API,
future AI agents) goes through the same rules.

## Availability (`scheduling/availability.py`)

`AvailabilityService(organization, service, location=None, public=False)` answers both "which
times are free?" (`get_available_slots(start_date, end_date, staff=None)`) and "is this time
free?" (`validate_slot(staff, start, ignore_booking=None)`), from the same calculation. The
location defaults to the organization's default location; all times are that location's.

A provider's free time on a date:

1. their weekly hours for the weekday (`WeeklyAvailability` rows for this location, or rows
   without a location, which apply at any location they work at);
2. limited by `AvailabilityException` rows for the date (off all day, or only certain times);
3. limited by the location's opening hours, when it has any;
4. minus location closures and organization holidays (all day, or certain times);
5. minus approved time off and active appointments. An appointment blocks its own time plus,
   on each side, the larger of its service's buffer and the new service's buffer. Buffers only
   separate appointments; they aren't needed at the edges of working hours;
6. nothing on a day they already have `max_daily_appointments` appointments.

A slot starts on a 15-minute grid on the location's wall clock and must fit entirely in free
time, using the provider's own duration (`StaffServiceOffering.custom_duration_minutes`) when
set. Only providers from `list_providers_for(service, location, public=)` are considered. Public
callers are also held to the service's `min_notice_minutes` and `max_advance_days`; the team is
only kept out of the past, and `validate_slot` accepts team times off the grid.

Daylight-saving changes are handled by working in UTC instants: on a spring-forward day the
missing hour has no slots, and on a fall-back day the repeated hour is offered twice (two
distinct instants). Everything is loaded up front: a calculation uses at most `QUERY_BUDGET`
(10) queries whatever the range and number of providers (tested with 10 providers over 30 days).

Where it is used: the public slot API, and `create_booking`/`reschedule_booking` for every
caller (since M4.1). The public booking form also calls it first, to show friendly errors.

## Lifecycle

| From | Allowed next statuses |
|---|---|
| pending | confirmed, cancelled, rejected |
| confirmed | checked_in, cancelled, no_show |
| checked_in | in_progress, completed, no_show |
| in_progress | completed |
| completed, cancelled, no_show, rejected | *(terminal)* |

Only `pending` and `confirmed` appointments can be rescheduled. Every status change writes a
`BookingStatusHistory` row with who made it and an optional note.

## Errors

Services raise `core.exceptions.DomainError` (HTTP 400) or `ConflictError` (HTTP 409). The API
returns them as `{"detail": "...", "code": "..."}`.

| Code | HTTP | Meaning |
|---|---|---|
| `in_past` | 400 | Start time is in the past |
| `too_soon` | 400 | Online booking: inside the service's minimum notice |
| `too_far` | 400 | Online booking: beyond how far ahead the service can be booked |
| `not_offered` | 400 | The provider doesn't offer the service at this location (or isn't bookable online) |
| `invalid_status` | 400 | Unknown status value |
| `not_found` | 400 | Service, staff, location or customer from another organization (or inactive) |
| `past_cancellation_deadline` | 400 | A customer cancelling or rescheduling inside the service's cancellation deadline |
| `invalid_source` | 400 | Unknown booking source |
| `slot_unavailable` | 409 | The time isn't free: outside working or opening hours, closed, time off, or overlapping another appointment (with buffers) |
| `invalid_transition` | 409 | The lifecycle doesn't allow the change (for example, cancelling a completed appointment) |
| `idempotency_key_reused` | 409 | The idempotency key was already used for a different booking |
| `plan_limit` | 409 | The plan's monthly booking limit is reached |

## Concurrency and the no-overlap guarantee

Two layers, so a double booking needs both to fail at once:

1. **The lock.** Every create and reschedule first locks the provider's `StaffProfile` row
   (`bookings.services.lock_staff`), then checks the time against committed data. The staff
   row exists even when the requested time is empty, so two simultaneous requests for the same
   provider queue on it: exactly one wins, and the other gets 409 `slot_unavailable`. Status
   changes and cancellations lock only the booking row. The lock order is always staff, then
   booking, so these paths can't deadlock.
2. **The database (PostgreSQL, M4.2).** The exclusion constraint `booking_staff_no_overlap`
   (migration `bookings/0010`, `btree_gist`) refuses two active appointments (pending,
   confirmed, checked in, in progress) of one staff member whose `[start, end)` ranges overlap,
   whatever code path writes them. The booking service turns a violation into 409
   `slot_unavailable`. It covers the appointment itself, not buffers: the gap needed between
   two appointments is the larger of their buffers, which a per-row range can't express, so
   buffers stay an engine rule (under the lock).

Before the constraint is added, the migration looks for existing overlaps and stops with a list
of them if there are any. `python manage.py check_booking_overlaps` prints the full list (and
exits with status 1) so they can be cancelled or moved first. Other databases skip the
constraint (development only).

Tests:

- `tests/test_booking_engine.py`: two real connections and an observed `pg_blocking_pids`
  wait (same slot, overlapping slots, reschedule against create, competing status changes).
- `tests/test_booking_service.py`: the constraint refuses overlapping rows written directly,
  allows back-to-back and inactive ones, maps to 409 inside the service, and alone stops a race
  in which the lock is disabled and both requests pass validation at the same moment; the
  overlap check and the migration's pre-check.
