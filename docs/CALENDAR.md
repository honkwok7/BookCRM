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

**Working hours** are shaded light green behind the appointments in the day and week views,
so free time in working hours stands out. The day view shades each provider's column with
their own hours. The week view shades the chosen provider's hours, or, with "Everyone", the
hours when anyone shown is working. The hours are the weekly hours (for the chosen location,
or every location), limited by day exceptions and the location's opening hours, minus
closures, holidays and approved time off (`scheduling/working_hours.py`, a fixed number of
queries). The grid widens to show hours outside 8:00–20:00. The month view isn't shaded.

Double-clicking an appointment opens its details in a dialog: time, provider, location, the
customer (linked to their CRM record when you can browse customers), price, source, notes and
history. A single mouse click does nothing, so the double-click doesn't also navigate. Enter
on a focused appointment and a tap on a touch screen open the dialog at once, and
Ctrl/Cmd-click opens the appointment's own page in a new tab. The dialog also offers the
actions its status allows: confirm, check in, start, check out, no-show, reject,
and cancel with a reason. The actions go through the booking service, so the lifecycle and
timing rules apply ([BOOKING_ENGINE.md](BOOKING_ENGINE.md#lifecycle)). The history records the
source as `reception` for receptionists and `staff` for everyone else. After an action the
calendar refreshes itself.

## Drag and drop

Members with `appointments.manage` can drag a pending or confirmed appointment to a new time
in the day and week views, or to another day in the month view, where it keeps its time. The card snaps to 15-minute rows and shows the time it would move
to. In the week view it can go to another day; in the day view it stays in its provider's
column, because moving keeps the same provider. Dropping it opens a dialog that shows the old
time and the new day, with a list of the provider's free times that day. The dropped time is
chosen when it's free; otherwise the dialog says so and asks for another. The list lets you
fine-tune the time, which matters most in the month view, where a drop keeps the old time.
The dialog can also cancel the appointment instead (with an optional reason). **Move**
reschedules it through `reschedule_booking`, which applies every
check a new booking gets (working hours, closures, conflicts). A refusal is explained in the
dialog. **Keep the current time**, Escape or closing the dialog puts the card back.
Escape during a drag cancels it.

The browser sends the dropped wall time and the zone the calendar was drawn in
(`/app/appointments/<id>/move/?start=YYYY-MM-DDTHH:MM&tz=…`). The server checks the zone, and
refuses a time that a daylight-saving change skips. Drag and drop is mouse and pen only: on a
touch screen dragging scrolls the calendar, and the dialog's **Reschedule** button moves an
appointment instead. Without JavaScript the move
confirmation is an ordinary page.

## Booking from an empty spot

With `appointments.manage`, clicking an empty spot in the day or week view opens **New
appointment** in the dialog. A dashed "+ 2:00" marker follows the mouse and shows the time.
The form starts on that date and remembers the clicked time and provider: in the day view the
provider is the column's person, and in the week view the calendar's Provider filter. Once a
service is chosen, it selects that provider if they offer the service. It then selects the free
time at the clicked time, or the next free time after it. In the month view, clicking a day's
empty space opens the form on that date. Past days can't be clicked. After booking, the dialog
closes and the calendar redraws. Booked from anywhere else, the form still opens the new
appointment's page (`/app/appointments/new/?date=&at=&staff=&location=`).

## Access

- The page and the sidebar link are for members with `appointments.view_all` or
  `appointments.manage`.
- With `appointments.view_all`, a member sees the whole organization. Without it (providers),
  they see only appointments assigned to them, and the provider filter lists only themselves.
  An appointment outside that scope is a 404 in the dialog too, and can't be moved.
- Actions and drag and drop need `appointments.manage`.
- Internal notes appear in the dialog only with `customers.notes.private`. The feed never
  includes them.

## Events feed

`GET /app/calendar/events.json?start=YYYY-MM-DD&end=YYYY-MM-DD` (end exclusive, at most 62
days) accepts the page's filters and uses the same scoping. It returns `timezone` and
`events`. Each event has these minimal fields: `id`, `title` (customer · service), `start`,
`end`, `status`, `staff`, `service`, `location` and `url`.

## How it's built

The layout is computed in `bookings/calendar.py` and rendered as plain HTML with CSS grid, so
it works without JavaScript. htmx swaps the calendar on navigation and filter changes and
loads appointments into the page dialog. Double-click and drag and drop live in
`static/js/app.js` (delegated from the document, because htmx replaces the calendar). There's no inline style (the Content Security Policy forbids it), and no
calendar library (FullCalendar's resource views need a commercial licence and it injects
styles). Each row is 15 minutes. An event's row, span and lane become Tailwind grid classes,
listed with `@source inline(...)` in `static/src/app.css`. At most `MAX_LANES` (24)
appointments sit side by side in one column; more at the same moment share lanes. On a
daylight-saving day an appointment's row follows the wall clock (the hour labels) and its
length is its real duration, so one in the repeated hour still shows and one across the
missing hour isn't doubled. The page uses a fixed number of queries however many appointments
it shows, and a test checks this.

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
