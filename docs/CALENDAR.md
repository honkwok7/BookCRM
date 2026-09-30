# Calendar

`/app/calendar/` shows appointments by day, week or month (M4.4).

## Views

| View | Layout |
|---|---|
| Day | One column per provider (at the chosen location, if any). This is the "resource" view |
| Week | One column per day. Overlapping appointments of different providers sit side by side, and each provider has a colour |
| Month | Up to three appointments per day, then "+N more", which opens that day |

The filters are location, provider, service and status. By default cancelled and rejected
appointments are hidden, and choosing a status shows only that status. Times are shown in the
chosen location's time zone, or the organization's. The grid shows 8:00 to 20:00 and widens
to include any appointment outside those hours.

Clicking an appointment opens its panel: time, provider, location, the customer (linked to
their CRM record when you can browse customers), price, source, notes and history. It also
offers the actions its status allows: confirm, check in, start, check out, no-show, reject,
and cancel with a reason. The actions go through the booking service, so the lifecycle and
timing rules apply ([BOOKING_ENGINE.md](BOOKING_ENGINE.md#lifecycle)). The history records the
source as `reception` for receptionists and `staff` for everyone else. After an action the
calendar refreshes itself.

## Access

- The page and the sidebar link are for members with `appointments.view_all` or
  `appointments.manage`.
- With `appointments.view_all`, a member sees the whole organization. Without it (providers),
  they see only appointments assigned to them, and the provider filter lists only themselves.
  An appointment outside that scope is a 404 in the panel too.
- Actions need `appointments.manage`.
- Internal notes appear in the panel only with `customers.notes.private`. The feed never
  includes them.

## Events feed

`GET /app/calendar/events.json?start=YYYY-MM-DD&end=YYYY-MM-DD` (end exclusive, at most 62
days) accepts the page's filters and uses the same scoping. It returns `timezone` and
`events`. Each event has these minimal fields: `id`, `title` (customer · service), `start`,
`end`, `status`, `staff`, `service`, `location` and `url`.

## How it's built

The layout is computed in `bookings/calendar.py` and rendered as plain HTML with CSS grid, so
it works without JavaScript. htmx swaps the calendar on navigation and filter changes and
loads the panel. There's no inline style (the Content Security Policy forbids it), and no
calendar library (FullCalendar's resource views need a commercial licence and it injects
styles). Each row is 15 minutes. An event's row, span and lane become Tailwind grid classes,
listed with `@source inline(...)` in `static/src/app.css`. The page uses a fixed number of
queries however many appointments it shows, and a test checks this.

Drag-and-drop rescheduling isn't built. It would call `reschedule_booking`.

## Appointment screens (M4.5)

| Screen | What it does |
|---|---|
| `/app/appointments/` | A list with search (customer name, email, phone, reference) and filters: when (upcoming, today, past, all), status and provider. Upcoming and today show active appointments unless a status is chosen |
| `/app/appointments/new/` | A new appointment, in the page dialog |
| `/app/appointments/walk-in/` | Someone walks in: book from the next minute with the chosen provider, or the first one free, and check them in straight away |
| `/app/appointments/<id>/reschedule/` | A new date and time with the same provider. The appointment's own time doesn't block its new one. The old appointment is closed as rescheduled |

**The new appointment form:**

- **Customer.** Search the customers you may read, or enter a new customer with a name and
  an email or phone number. Phone-only customers are fine.
- **Location, service, provider and date.** Location defaults to the default location. The
  provider can be "First available" when several offer the service.
- **Times.** A list of free times, each showing who is free.
- **Rebuilding the choices.** Every change re-posts the form to rebuild the choices (htmx,
  `refresh`). Without JavaScript the Update button does the same.
- **Booking.** It goes through the booking service with the team's rules (no online notice
  or booking window), so the time is checked again under the lock.

**Access and sources:**

- The screens need `appointments.manage` to book.
- Providers without `appointments.view_all` see and book only their own appointments, and
  can look up only their own customers.
- The source is `reception` for receptionists and `staff` for everyone else.
- A reschedule keeps the appointment's original source, and its history records who moved it.
