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

Creation re-checks for overlaps inside a transaction with `select_for_update()`. **This does not
yet guarantee that two simultaneous requests can't both book an empty slot.** When nothing
overlaps, no row is locked. Planned (M4.1/M4.2):

1. Lock the provider's `StaffProfile` row before checking, the parent-row pattern already
   proven in the legacy `appointments/transactions.py`.
2. A PostgreSQL exclusion constraint on (provider, time range including buffers) for active
   statuses, as the final guarantee. A violation maps to 409 `slot_unavailable`.
3. Validation against availability, notice and advance limits, and buffers. Today the engine
   doesn't check the provider's working hours.
