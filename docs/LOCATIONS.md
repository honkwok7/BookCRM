# Locations

A location is a place where an organization sees customers: a clinic, a branch, a studio. The
`locations` app holds the models, `locations/services.py` for writes and
`locations/selectors.py` for reads. The API, the web app, `seed_demo` and future AI agents
all go through them.

## Rules

| Rule | Where it is enforced |
|---|---|
| Every organization has **exactly one default location**. It is created as "Main" when the organization is created, from the organization's address, time zone, phone and email. | `locations.signals` (new organizations), migration `locations/0002` (existing ones), and a partial unique index (at most one default per organization). `loaddata` skips signals: run `manage.py ensure_default_locations` after loading organization fixtures. |
| The default location is **always active**. It can't be deactivated or deleted: make another location the default first. | Service (`default_location`, 409) and a database check constraint. |
| Only an active location can become the default. | Service (`inactive`, 409). |
| **Active** locations count towards the plan's `maximum_locations`. Creating or reactivating a location beyond the limit is refused. Inactive locations don't count. An organization without a subscription has no limit. | Service (`plan_limit`, 409) through `subscriptions.services.enforce_plan_limit(organization, "locations")`. The organization row is locked, so two parallel requests can't both take the last place. |
| A location's `slug` is unique in its organization and never changes, because it will be used in public booking links (M4.6). | Service and a unique constraint. |
| The time zone must be a known IANA zone (`America/Toronto`). A new location defaults to the organization's time zone. | Service (`invalid_timezone`) and the model validator. |
| `booking_settings` is reserved for per-location online booking rules (M4.6). It is read-only for now. | API serializer. |

## Opening hours

Weekly hours are stored as periods (`weekday`, `opens_at`, `closes_at`) in the location's
time zone, Monday = 0 (the same numbering as `date.weekday()` and staff availability).

- A day has **at most two periods**, for example 09:00–12:00 and 13:00–18:00. The periods can't
  overlap; back-to-back is fine.
- A day without periods is closed.
- A location with **no hours at all** hasn't set any, and does not limit bookings; each
  provider's availability decides on its own. The availability engine (M3.4) intersects staff
  availability with location hours only when hours are set.
- Periods can't run past midnight. A late night is two periods on consecutive days.
- `set_location_hours` replaces the whole week in one call and audits
  `location.hours_updated` with the before and after hours (only when they changed).

## Closures

A closure covers one or more days (`start_date`–`end_date`), either **all day** or **between
two times on each of those days** (for example 12:00–14:00 for a staff meeting). Organization-wide
holidays remain in `scheduling.OrganizationHoliday`; a closure applies to one location.

## Writes (`locations/services.py`)

| Function | Audit action |
|---|---|
| `create_location` | `location.created` |
| `update_location` | `location.updated`, with the changed fields |
| `set_default_location` | `location.updated`, with `is_default` and the previous default |
| `delete_location` | `location.deleted`. Refused for the default; a location referenced elsewhere (from M3.2 on) gives `in_use` and should be deactivated instead. |
| `set_location_hours` | `location.hours_updated` |
| `create_closure`, `update_closure`, `delete_closure` | `location_closure.created/updated/deleted` |
| `ensure_default_location` | Not audited: it runs as part of creating an organization. |

## Permissions

| Capability | Owner | Manager | Receptionist | Staff |
|---|---|---|---|---|
| `locations.view`: see locations, hours and closures | ✅ | ✅ | ✅ | ✅ |
| `locations.manage`: change them | ✅ | ✅ | – | – |

Customers have neither. See [PERMISSIONS.md](PERMISSIONS.md).

## Screens

- `/app/locations/`: every location with its address, today's hours in the location's time
  zone, time zone and online booking status. Managers also see how many active locations their
  plan allows; the "New location" button is hidden at the limit.
- `/app/locations/<id>/`: the weekly hours (today highlighted), upcoming closures (past ones
  folded away), details, and for managers the Edit, Edit hours, Add closure, Remove and Make
  default actions. The forms open in the page dialog and also work as plain pages without
  JavaScript.

## API

See [API.md](API.md#locations).
