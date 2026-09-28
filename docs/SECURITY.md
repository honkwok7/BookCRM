# Security

An honest status of the security controls. No regulatory compliance (HIPAA, PHIPA, GDPR, …) is
claimed; that would need a separate formal review.

## Status

| Control | Status | Where |
|---|---|---|
| Email-first custom user, strong password validators | ✅ | `accounts/models.py`, `AUTH_PASSWORD_VALIDATORS` |
| JWT rotation + blacklist | ✅ | `SIMPLE_JWT` |
| Tenant isolation: membership-backed tenant resolution, no superuser bypass | ✅ | `organizations/tenancy.py`, [MULTI_TENANCY.md](MULTI_TENANCY.md) |
| Capability-based authorization on every tenant endpoint | ✅ | `organizations/permissions.py`, [PERMISSIONS.md](PERMISSIONS.md) |
| One authorization model (memberships + capabilities); the legacy global `User.role` is removed | ✅ | M1.6 |
| Relation fields limited to the tenant's own rows; no `fields="__all__"` | ✅ | `core/api.py`, `core/test_api_contract.py` |
| Appointment changes only through the booking service | ✅ | `bookings/services.py` |
| Audit logging of important actions (append-only, scrubbed) | ✅ | `core/audit.py`, see below |
| Environment-based config; refuses the default `SECRET_KEY` when `DEBUG` is off | ✅ | `config/settings.py` |
| CSRF, clickjacking (`DENY`), `nosniff`, auto-escaping templates | ✅ | Django defaults and settings |
| `manage.py check --deploy` in CI | ❌ | Planned for M11.1 |
| HSTS, secure cookies and SSL redirect enforced in production | ❌ | Configurable through env vars today; enforced in M11.1 |
| Login and password-reset rate limiting / lockout | ❌ | Throttle scopes exist but aren't applied yet (M2.5) |
| Double-booking prevention under concurrency (staff-calendar lock, PostgreSQL race tests) | ✅ | `bookings/services.py`, `tests/test_booking_engine.py` |
| Database-level exclusion constraint as a second guarantee | ❌ | M4.2 (see [BOOKING_ENGINE.md](BOOKING_ENGINE.md)) |
| Upload validation (size, type) | ❌ | M11.1 |
| Customer data export / anonymization | ❌ | M11.1 |

## Audit logging

`core.audit.record_audit(AuditAction.X, organization=, actor=, target=, metadata=, changes=)` is
the only way to write audit entries.

- **Catalogue of actions:** `AuditAction`, for example `customer.updated` or
  `booking.cancelled`. Unknown actions are rejected.
- **What is recorded:**
  - organization;
  - acting user and **actor type** (`user`, `system`, `platform_admin`; `api_key` and
    `ai_agent` are reserved for M10.2);
  - `impersonator`, for support impersonation (M5.5);
  - target type and id;
  - metadata, and a field-level `changes` diff for updates;
  - client IP and user agent.
- **Coverage:**
  - booking create, cancel, status change and reschedule;
  - organization profile updates, suspension and reactivation;
  - invitations created and accepted;
  - email verification and password reset;
  - create, update and delete for services, categories, staff, availability, exceptions,
    time off, holidays, customers and waitlist entries.

  `core/test_audit.py` fails if a new create/update/delete API endpoint isn't audited.
- **Atomic:** each write and its audit row are saved in the same transaction. A failed
  write leaves no audit row.
- **No secrets:** metadata goes through a scrubber that redacts keys that look secret
  (password, token, secret, api_key, card, cvv, …) at any depth. Passwords and tokens are
  never passed in on purpose.
- **Minimal personal data:** for customers, waitlist entries and staff phone numbers, changed
  fields are recorded as `"changed"` **without values**. Audit history then survives
  customer anonymization. Invitation entries record the role, not the invitee's email.
- **Append-only:** `AuditLog.save()` refuses updates and `delete()` refuses deletion. Django
  admin is read-only for audit entries.
- **Client IP:** `REMOTE_ADDR` by default. Set `TRUSTED_PROXY_COUNT` to the number of reverse
  proxies in front of the app, so that the proxy-appended `X-Forwarded-For` entry is used. The
  client-controlled left-most entry is never trusted.
- **Reading:** `GET /api/v1/audit-logs/` for members with `audit.view` (owners by default),
  scoped to their organization. Filters: `action`, `object_type`, `object_identifier`,
  `actor_type`.
