# Services

A service is something customers book: a 60-minute massage, a first assessment. Services are
grouped into categories. `services/services.py` holds the writes and `services/selectors.py` the
reads; the API, the web app, `seed_demo` and future AI agents all go through them.

## Fields worth knowing

| Field | Meaning |
|---|---|
| `duration_minutes`, `price` | Defaults; a provider can have their own (see [STAFF.md](STAFF.md)). |
| `buffer_before_minutes`, `buffer_after_minutes` | Time kept free around each appointment (used by the availability engine, M3.4). |
| `locations` | Where it is offered. **Empty means every location**, including locations added later. |
| `required_provider_type` | Only staff with this `provider_type` (ignoring case) can offer it. Empty: anyone. |
| `is_public` | Bookable online (the UI says "Customers can book it online"). Off: only the team can book it. |
| `is_active` / `is_archived` | Inactive services can't be booked. Archived services are hidden everywhere and don't count towards the plan, but their appointment history stays. |
| `min_notice_minutes`, `max_advance_days` | How late and how far ahead online booking is open. |
| `cancellation_deadline_hours`, `rescheduling_deadline_hours`, `cancellation_policy` | Change rules and the policy text shown to customers. |
| `tax_rate` | A percentage, recorded for now; a tax model comes with billing. |
| Category `color`, `sort_order` | Categories are listed by position, then name. |

## Rules

| Rule | Where it is enforced |
|---|---|
| Names are required; durations are at least one minute; prices and tax rates aren't negative (tax at most 100); capacity is at least 1. | Service layer (`name_required`, `invalid_duration`, `invalid_price`, `invalid_tax_rate`, `invalid_capacity`). |
| Slugs are unique per organization and don't change when the service is renamed. | Service layer and a unique constraint. |
| Services that aren't archived count towards the plan's `maximum_services`; creating or unarchiving beyond it is refused. | Service layer (`plan_limit`, 409), organization row locked. |
| The category and locations must belong to the same organization. | Service layer and the API's tenant-scoped relation fields. |
| A service with appointments can't be deleted; archive it instead. | `delete_service` (`in_use`, 409). |
| Category names are unique per organization, ignoring case. Deleting a category keeps its services, without a category. | `create_category`/`update_category` (`duplicate`, 409), `delete_category`. |
| A provider can only offer a service if they have its required type, and only at a location where the service is offered. | `staff.services` (`provider_type_mismatch`, `service_not_at_location`). |

Changing a service's required type or locations later doesn't delete offerings that no longer
fit; `list_providers_for` simply leaves those providers out, and saving that person's services
shows the problem.

## Which services can be booked where

`services_bookable_at(organization, location=None, public=...)` lists active, unarchived
services (only those bookable online when `public`) offered at the location. The public booking
wizard (M4.6) uses it for its service step.

## Screens

`/app/services/`: services grouped by category, in category order, with duration and buffers,
price, where they are offered, how many providers offer them (a warning when none do) and their
booking status. Filters: search, location, status (not archived, archived, inactive, all).
Managers add and edit services and categories in the page dialog. Who offers each service is set
on each person's page under Staff.

Viewing needs `services.view` (every team role); changes need `services.manage` (owners and
managers).

## API

See [API.md](API.md#services-staff-scheduling).
