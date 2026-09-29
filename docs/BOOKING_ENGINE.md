# Booking Engine

"Booking" is the model name; the UI and docs call it an **appointment**.

## One entry point

Every change to an appointment goes through `bookings/services.py`:

| Function | What it does |
|---|---|
| `create_booking(...)` | Rejects past times, checks for overlaps with the provider's active appointments, creates or reuses the customer, writes status history and an audit entry, and queues the confirmation email **after commit** |
| `cancel_booking(booking, actor, reason)` | Locks the row, refuses appointments that are already finished, records who cancelled and why, and queues the cancellation email |
| `change_booking_status(booking, new_status, actor, note)` | Validates the transition (below), writes status history and an audit entry. Cancelling is delegated to `cancel_booking` |
| `reschedule_booking(booking, new_start, actor)` | **One transaction**: closes the old appointment as cancelled ("Rescheduled") and creates a new one linked through `rescheduled_from`. If the new time is rejected, nothing changes. The customer's account link is kept |

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

Where it is used today: the public slot API, and validation of bookings made on the public page
or through the API by customers (codes below). Team bookings are validated from M4.1, when
`BookingService` calls `validate_slot`; until then they are only checked for overlaps, and use
the service's own duration rather than the provider's custom one.

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
| `slot_unavailable` | 409 | The time isn't free: outside working or opening hours, closed, time off, or overlapping another appointment (with buffers) |
| `invalid_transition` | 409 | The lifecycle doesn't allow the change (for example, cancelling a completed appointment) |

## Concurrency: current state and plan

Every create and reschedule first locks the provider's `StaffProfile` row
(`bookings.services.lock_staff`), then re-checks overlaps against committed data. The staff
row exists even when the requested time is empty, so two simultaneous requests for the same
provider queue on it: exactly one wins, and the other gets 409 `slot_unavailable`. Status
changes and cancellations lock only the booking row. The lock order is always staff, then
booking, so these paths can't deadlock.

`tests/test_booking_engine.py` proves this on PostgreSQL with two real connections and an
observed `pg_blocking_pids` wait (same slot, overlapping slots, reschedule against create,
competing status changes). With the staff lock removed, the double-booking races fail.

Still planned:

1. **M4.2:** a PostgreSQL exclusion constraint on (provider, time range including buffers) for
   active statuses, as a database-level guarantee that also covers writes outside the
   service. A violation maps to 409 `slot_unavailable`.
2. **M4.1:** `create_booking` and `reschedule_booking` call `validate_slot` for every caller,
   and use the provider's own duration and price.
