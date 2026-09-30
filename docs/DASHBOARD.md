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
| Estimated revenue (this month, and the period) | The sum of completed appointments' `price_snapshot`. An estimate, not payments; labelled so. |
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
| Providers now | Busy (with a customer until a time), Free (with their next appointment), or Off (time off now, or outside today's weekly hours) |
| Cancelled today | Appointments cancelled today, for whatever date they were booked |
| Waitlist | How many people are waiting for a time at this location, with a link |
| Next free time | The first free time per service in the next 7 days (the team's view, with no online notice), up to 8 services |

- **Live:** the page refreshes itself every 30 seconds through htmx polling (the
  `#reception-live` partial). That refresh takes 6 queries of its own
  (`bookings.reception.front_desk`), and the whole request is tested at 14 or fewer. The next
  free times load separately, refresh every 5 minutes and are cached for a minute, because
  they run the availability engine once per service.
- **Actions** are plain forms posting to the appointment action view with `next` pointing back
  here. The action view only follows a `next` that is a page of this app (`/app/…` on the same
  host); anything else goes to the appointment. Everything works without JavaScript.
- **Walk-in:** from the front desk, click Walk-in, then submit the form. The appointment is
  created and checked in (the plan's target was three interactions or fewer).
- **Settings and billing:** receptionists hold neither `organization.manage` nor
  `billing.manage`, and the front desk links to neither. The settings and subscription pages
  themselves come in later phases, with their own tests.
