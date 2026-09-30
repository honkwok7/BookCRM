# Waitlist

Customers who can't find a time join the waitlist. When an appointment is cancelled, the
people it suits are emailed automatically (M4.7).

## Joining

- **Online.** On the booking page, the time step links to "Join the waitlist"
  (`/book/<slug>/waitlist/`). It uses the location, service and provider already chosen, plus
  a time of day (any, morning, afternoon, evening), optional dates, and a name, email and
  phone. The page has the same honeypot and per-IP rate limit as booking and respects "guest
  booking off". Afterwards it only says "You're on the waitlist": nothing about other entries
  is ever shown.
- **At reception.** `/app/waitlist/` (needs `waitlist.manage`) lists waiting and notified
  entries, filterable by status and service. The "Add to waitlist" dialog takes the same
  preferences. An entry can be closed.
- **Through the service.** Both use `bookings.waitlist.join_waitlist`. It checks that
  everything belongs to the organization, requires an email address (that's how people are
  told), links the entry to the CRM customer (found or created like a booking's), and sets
  `expires_at` to the day after the last preferred date, or 60 days from now. Joining again
  for the same service updates the waiting entry. A database constraint
  (`waitlist_one_waiting_entry_per_customer`) keeps it to one waiting entry per customer and
  service even when two joins race. Preferences that could never match (a provider who doesn't
  offer the service there, a location where nobody does) are refused with
  `invalid_preferences`.
- **Over the API.** `/api/v1/waitlist/` joins through the same service and closes with
  `close/`. It has no generic edit or delete, so status and notification fields can't be set by
  hand.

## When a time frees up

- **Queuing.** `cancel_booking` (and a reschedule, when the new time no longer overlaps the
  old one) calls `schedule_matching`, which queues the `bookings.tasks.notify_waitlist` task
  after the transaction commits. Past times are skipped, and if the provider has been booked
  over the freed time again before the task runs, nobody is emailed.
- **Matching.** `matching_entries` finds waiting entries for the same service whose location
  (or any location), provider (or any) and date range include the freed time, whose time of
  day contains its start, and that haven't expired. The oldest come first.
- **Emailing.** `notify_matching_entries` emails the first `NOTIFY_LIMIT` (5). Each entry is
  claimed atomically (waiting → notified, `notified_at`), so an entry is emailed once, even
  when two cancellations race.
- **The email** (`emails/waitlist_slot_available`) names the service, the time and the place,
  and links to the booking page. It never names the customer who cancelled or their
  reference. The notification log records the waitlist entry, so the sent email appears on
  the waiting customer's timeline.

The task takes the organization id explicitly and does nothing for a booking of another
organization.

## Existing entries

Migration `bookings/0013` links existing entries to the CRM customer with the same email in
the same organization. Migration `bookings/0014` closes duplicate waiting entries (the oldest
keeps its place) before adding the one-waiting-entry constraint.
