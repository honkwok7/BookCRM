# Organization dashboard (M5.1)

`/app/dashboard/`, where owners, managers and receptionists land after signing in. Providers
land on `/staff/dashboard/`.

## Who sees what

| Part | Needs | Shows |
|---|---|---|
| Upcoming appointments | Team membership | Appointments that haven't finished yet (the current one, then the next ones), up to 10. Providers without `appointments.view_all` see only their own. Not cached. |
| Analytics | `reports.view` (owners and managers) | Everything below, with the filter bar |

Receptionists get the operational part only. They land on the reception dashboard instead
(below).

## Analytics widgets

Every number comes from `dashboard/reporting.py` (`dashboard_report`):

| Widget | Definition |
|---|---|
| Appointments today / this week | Appointments still on (not cancelled or rejected) starting today, or this Monday–Sunday, in the organization's time zone. |
| New customers | Customer records created in the period, not anonymized. With a location or provider filter: only those who booked with that selection. |
| Cancellation rate, no-show rate | Among appointments in the period whose time has passed (rejected ones left out). Shown only from `MIN_SAMPLE` (20) appointments; below that the widget says "Not enough data yet". |
| Estimated revenue (this month, and the period) | The sum of completed appointments' `price_snapshot`, for appointments that start by the end of today (one completed early for tomorrow counts tomorrow). An estimate, not payments; labelled so. |
| Popular services, top providers | The top 5 by appointments still on in the period. Top providers also show completed appointments and estimated revenue. |
| Appointments per day | One bar per day of the period. |

**Filters:** the period (last 7, 30 or 90 days), the location (when there is more than one), and
the provider. The page checks every id against the organization, so an id from elsewhere is
ignored rather than trusted. The filter bar refreshes only `#dashboard` through htmx and updates
the address bar; without JavaScript it is a plain form.

## Performance

- **Queries:** the analytics take 5 queries using conditional aggregation (`Count` and `Sum`
  with `filter=`), whatever the amount of data. The whole page renders in 12 queries or
  fewer, and a test checks this.
- **Caching:** the analytics are cached for 60 seconds per organization, day and filter set
  (`CACHE_SECONDS`), so a number can be up to a minute old. Upcoming appointments are always
  live.

## The chart

The volume chart is server-drawn SVG: bars are `<rect>` elements with attribute heights. It
has no inline styles (the Content Security Policy forbids them) and no chart library. A
visually hidden table gives screen readers the same numbers.

The plan named Chart.js. That can come with the reports (M9) if interactive charts are
needed.

# Reception dashboard (M5.2)

`/app/reception/` is the front desk's page and where receptionists land after signing in. It
needs `appointments.manage` and `appointments.view_all`, so receptionists, managers and owners
can use it; providers and customers get 403. It shows one location at a time: the default
location, or the one chosen with the location filter when the organization has several.

| Section | Shows |
|---|---|
| Quick actions | Walk-in, new appointment, new customer, find a customer, calendar |
| Today | Every appointment today (all statuses except rejected requests), with Check in, Check out and No-show buttons where the workflow allows them, plus Reschedule |
| Waiting | Who has checked in, in check-in order |
| Providers now | Busy (with a customer until a time, at any location, including an appointment that started before midnight), Free (with their next appointment here), or Off (time off now, or outside today's weekly hours) |
| Cancelled today | Appointments cancelled today, for whatever date they were booked |
| Waitlist | How many people are waiting for a time at this location, with a link |
| Next free time | The first free time per service in the next 7 days (the team's view, with no online notice), up to 8 services. It sits inside the live block, so changing location replaces it; the 30-second refresh keeps it (`hx-preserve`) |

- **Live:** the page refreshes itself every 30 seconds through htmx polling (the
  `#reception-live` partial). That refresh takes 6 queries of its own
  (`bookings.reception.front_desk`), and the whole request is tested at 14 or fewer. The next
  free times load separately, refresh every 5 minutes and are cached for a minute, because
  they run the availability engine once per service.
- **Actions** are plain forms posting to the appointment action view with `next` pointing back
  here. The action view only follows a `next` that is a page of this app (`/app/…` or
  `/staff/…` on the same host); anything else goes to the appointment. Everything works without JavaScript.
- **Walk-in:** from the front desk, click Walk-in, then submit the form. The appointment is
  created and checked in (the plan's target was three interactions or fewer).
- **Settings and billing:** receptionists hold neither `organization.manage` nor
  `billing.manage`, and the front desk links to neither. The settings and subscription pages
  themselves come in later phases, with their own tests.

# Provider area (M5.3)

`/staff/` is the signed-in member's own work. Every page is scoped to **their own staff
profile, whatever their role**: an owner who also takes appointments sees everyone in `/app/`,
but only their own here. Members without a staff profile are refused (403), except staff-role
members, who land on an empty "My day".

| Page | What it shows |
|---|---|
| `/staff/dashboard/` (My day) | "Good morning, {first name}", what is on now or next, today's count, this week's count, customers seen in the last 30 days; today's appointments with Check in, Start, Check out and No-show; the rest of the week; cancellations in the last 7 days; upcoming time off. 4 queries of its own (`staff.provider.provider_day`). |
| `/staff/calendar/` | The calendar, narrowed to their own appointments (a `staff=` for someone else is ignored). |
| `/staff/customers/` | The customer list, narrowed to customers assigned to them or seen by them. No "New customer" here. |
| `/staff/availability/` | Their weekly hours (read-only: a manager sets them), their time off, **Block time** and **Request time off**. |

**Time off** (`scheduling.services`; `TimeOff.approval_status` is now pending, approved,
rejected or cancelled; only approved time off blocks the calendar):

- **Block time:** part of one day, up to 12 hours, approved at once. Refused if the provider
  has an appointment then. It locks the provider's calendar row like booking does, so a block
  and a booking for the same time can't both succeed.
- **Request time off:** whole days, up to 90, pending until someone with `staff.manage`
  approves or rejects it on the staff member's Time off tab. The tab says how many appointments
  are booked then; approving doesn't move them.
- The provider can cancel a request, or approved time off that hasn't started.
- Every step is audited (`time_off.requested`, `.blocked`, `.approved`, `.rejected`,
  `.cancelled`).

Another provider's appointment is a 404 for a provider (the appointment page and its actions);
another provider's time off is a 404 on cancel.
