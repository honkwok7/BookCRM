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
| `invalid_status` | 400 | Unknown status value |
| `slot_unavailable` | 409 | The provider already has an active appointment overlapping this time |
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
2. **M4.1:** validation against availability, notice and advance limits, and buffers. Today
   the engine doesn't check the provider's working hours.
