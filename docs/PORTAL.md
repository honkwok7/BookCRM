# Customer portal (M5.4)

`/portal/` is for customers: people with an account who book with one or more businesses on
BookCRM. Code: `portal/` (selectors, policy, views, forms); templates in `templates/portal/`.

## Whose data

An account is a customer of a business when:

- a customer record there is linked to the account (bookings made while signed in with a
  verified email link it), or
- once the account's email is verified, bookings there were made with that email.

An unverified email matches nothing: anyone can register with somebody else's address.

**One business at a time.** Everything under `/portal/<slug>/` is filtered by that one business
(`portal.selectors`). The same email at two businesses gives two separate customer records;
each business's portal shows only its own appointments and details. A business the account
has nothing to do with, a suspended one, or another customer's appointment is a 404.

The portal never shows anything internal: internal notes, alerts, tags or team-only fields.

## Pages

| Page | What it does |
|---|---|
| `/portal/` | Every business, each with what is coming up, Book and All appointments |
| `/portal/<slug>/` | Next appointment, also coming up, recent visits, Book |
| `/portal/<slug>/appointments/` | Upcoming (with Reschedule and Cancel when allowed) and history |
| `/portal/<slug>/appointments/<id>/` | One appointment, and why it can't be changed online if it can't |
| `…/cancel/` | Confirm, with an optional reason |
| `…/reschedule/` | One day of free times with the same provider at the same place, starting on the appointment's own day |
| `/portal/<slug>/profile/` | Name, phone and communication preferences (email, text, marketing). The email stays the account's. |

**Book** is the business's public booking page (`/book/<slug>/`, the same wizard), which fills
in the account's name and email and links the booking to the account.

## Policy

Customers can cancel or move an appointment online until the service's cancellation deadline
(`cancellation_deadline_hours`, 24 by default). After that the page says to contact the
business. The booking services enforce it (`enforce_deadline=True`, `public=True`); a new time
must be one the public booking page would offer (the 15-minute grid, notice and window). Changes
are recorded with the source `customer_portal`, and cancellations record the account as who
cancelled.

Times show in the location's time zone.

## Demo

`seed_demo` creates `alex@example.test` / `Demo12345!`, a customer of both demo businesses.
