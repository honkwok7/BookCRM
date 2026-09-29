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
- `/api/v1/services/`
- `/api/v1/service-categories/`
- `/api/v1/staff/`
- `/api/v1/availability/weekly/`
- `/api/v1/availability/exceptions/`
- `/api/v1/availability/time-off/`
- `/api/v1/availability/holidays/`
- `/api/v1/availability/slots/available-slots/`

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
- `POST /api/v1/bookings/{id}/cancel/` `{"reason": "..."}`
- `POST /api/v1/bookings/{id}/reschedule/` `{"start_datetime": "..."}` returns the **new** appointment
- `POST /api/v1/bookings/{id}/update_status/` `{"status": "...", "note": "..."}` (needs `appointments.manage`)
- `/api/v1/customers/`
- `/api/v1/waitlist/`

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
