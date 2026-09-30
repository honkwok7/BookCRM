# API Guide

Base path: `/api/v1/`

## Auth
- `POST /api/register/`
- `POST /api/login/`
- `POST /api/token/refresh/`
- `POST /api/logout/`
- `POST /api/verify-email/`
- `POST /api/forgot-password/`
- `POST /api/reset-password/`

## Organization
- `GET/PATCH /api/organizations/current/`
- `GET /api/organizations/memberships/`
- `POST /api/organizations/invitations/`
- `POST /api/organizations/invitations/accept/`

## Services, Staff, Scheduling
- `/api/v1/services/`. Read: `services.view`; write: `services.manage`. `locations` (empty =
  every location), `required_provider_type`, `tax_rate` and `cancellation_policy` are
  writable; `slug` is read-only. Filters: `category`, `location`, `is_active`, `is_public`,
  `is_archived`. 409 `plan_limit` beyond the plan's service limit; 409 `in_use` when
  deleting a booked service. Writing `assigned_staff_members` sets who offers the service at
  all their locations (see [STAFF.md](STAFF.md)). Details: [SERVICES.md](SERVICES.md).
- `GET /api/v1/services/{id}/providers/?location=<id>`: staff who can be booked for the
  service (there).
- `/api/v1/service-categories/`: `name` (unique per organization, ignoring case; 409
  `duplicate`), `color`, `sort_order`. Deleting one keeps its services, without a category.
- `/api/v1/staff/`. Read: `staff.view`; write: `staff.manage`. `user` must be an active team
  member; `locations` replaces where they work. Filters: `location`, `is_active`,
  `is_accepting_bookings`, `online_booking_visible`. 409 `plan_limit` beyond the plan's staff
  limit; 409 `in_use` when deleting someone with appointments.
- `/api/v1/staff-offerings/` (CRUD): `staff`, `service`, `location` (empty = all their
  locations), `custom_duration_minutes`, `custom_price`, `is_active`; read-only
  `duration_minutes` and `price` are the values that apply. Filters: `staff`, `service`,
  `location`, `is_active`.
- `/api/v1/availability/weekly/`
- `/api/v1/availability/exceptions/`
- `/api/v1/availability/time-off/`
- `/api/v1/availability/holidays/`
- `GET /api/v1/availability/slots/available-slots/` (public, no sign-in):
  `?organization=<slug>&service=<id>&date=YYYY-MM-DD`, optional `staff` (else any provider),
  `location` (else the default location) and `end_date` (at most 31 days). Returns `timezone`,
  `location`, `slots` (start times in the location's time zone) and `availability` (each start
  with the providers free then, by public name, and when each would finish). Only services
  bookable online and staff visible online count; an unknown or hidden service, provider or
  location is 404. See [BOOKING_ENGINE.md](BOOKING_ENGINE.md#availability-schedulingavailabilitypy).
- `/api/v1/availability/weekly/` rows take an optional `location` (empty = any location the
  person works at).

## Locations
Read: `locations.view`. Write: `locations.manage`. Rules and error codes are in
[LOCATIONS.md](LOCATIONS.md).
- `GET/POST /api/v1/locations/`, `GET/PUT/PATCH/DELETE /api/v1/locations/{id}/`. The default
  location comes first. `slug`, `is_default` and `booking_settings` are read-only. Creating or
  reactivating beyond the plan limit gives 409 `plan_limit`; deactivating or deleting the
  default gives 409 `default_location`.
- `GET/PUT /api/v1/locations/{id}/hours/`: `PUT {"hours": [{"weekday": 0, "opens_at": "09:00",
  "closes_at": "17:00"}, ...]}` replaces the week (Monday = 0, at most two non-overlapping
  periods a day, `[]` clears the hours).
- `POST /api/v1/locations/{id}/make-default/`
- `/api/v1/location-closures/` (CRUD). Filters: `location`, `all_day`, `ends_after`,
  `starts_before`. `end_date` defaults to `start_date`; a closure that isn't `all_day` needs
  `start_time` and `end_time`.

## Bookings (appointments) and customers
- `GET/POST /api/v1/bookings/`, `GET /api/v1/bookings/{id}/`. There is **no** PUT, PATCH or DELETE (405).
  - `POST` takes `service`, `staff`, `start_datetime`, customer details, and optionally
    `location` (default: the default location) and `idempotency_key` (or an `Idempotency-Key`
    header: a retry returns the first booking). Every booking is checked against the
    provider's availability (409 `slot_unavailable` outside it; 400 `not_offered` when they
    don't offer the service there). Customers are also held to the online notice and window.
  - Responses include `location`; the team view adds `source`, `created_by` and the
    buffer snapshots.
- `POST /api/v1/bookings/{id}/cancel/` `{"reason": "..."}` (customers: 400 `past_cancellation_deadline` inside the service's deadline)
- `POST /api/v1/bookings/{id}/reschedule/` `{"start_datetime": "..."}` returns the **new** appointment
- `POST /api/v1/bookings/{id}/update_status/` `{"status": "...", "reason": "...", "note": "..."}` (needs `appointments.manage`)
- `POST /api/v1/bookings/{id}/check-in/` and `POST /api/v1/bookings/{id}/check-out/`: confirmed to checked in, and checked in or in progress to completed (needs `appointments.manage`; 400 `too_early` more than an hour before the start)
- `GET /api/v1/bookings/{id}/history/`: every status change with `reason`, `source`, `note`, `changed_by` (team only)
- `/api/v1/customers/`
- `/api/v1/waitlist/` (needs `waitlist.manage`): list, retrieve and `POST` to join. Joining goes through
  the waitlist service, so it links the CRM customer, updates the customer's waiting entry for the same
  service instead of adding another, and sets the expiry. `status`, `source`, `notified_at` and
  `expires_at` are read-only. There is no `PUT`/`PATCH`/`DELETE` (405).
  A provider or location that can never match is 400 `invalid_preferences`.
- `POST /api/v1/waitlist/{id}/close/`: stop waiting

Customers see a reduced appointment representation without `internal_notes`.
Lifecycle rules and error codes are in [BOOKING_ENGINE.md](BOOKING_ENGINE.md).

## Tenancy, permissions and errors
- **Organization:** the organization comes from your membership. `X-Organization-Slug` only picks among your own
  organizations ([MULTI_TENANCY.md](MULTI_TENANCY.md)). You can never set `organization` in a request body.
- **Permissions:** each endpoint checks capabilities ([PERMISSIONS.md](PERMISSIONS.md)).
- **Related ids** (`staff`, `service`, `category`, `assigned_staff_members`, `preferred_staff`, `user`)
  are accepted only when they belong to your organization. Otherwise the response is 400, as for an id that
  doesn't exist.
- **Business-rule errors:** `{"detail": "...", "code": "..."}` with 400 or 409. Validation errors use DRF's
  usual field-keyed format.

## Audit log
- `GET /api/v1/audit-logs/` is read-only and needs `audit.view` (owners by default). Filters: `action`, `object_type`, `object_identifier`, `actor_type`. See [SECURITY.md](SECURITY.md#audit-logging).

## Documentation
- Schema: `/api/schema/`
- Swagger UI: `/api/docs/`
- ReDoc: `/api/redoc/`
