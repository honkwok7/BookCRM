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

## Documentation
- Schema: `/api/schema/`
- Swagger UI: `/api/docs/`
- ReDoc: `/api/redoc/`
