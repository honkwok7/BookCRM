# Security

An honest status of the security controls. No regulatory compliance (HIPAA, PHIPA, GDPR, …) is
claimed; that would need a separate formal review.

## Status

| Control | Status | Where |
|---|---|---|
| Email-first custom user; password validators on registration and reset | ✅ | `accounts/serializers.py`, `AUTH_PASSWORD_VALIDATORS` |
| One account per mailbox: emails stored lower-case, unique index on `LOWER(email)`, login ignores case | ✅ | `accounts/models.py`, migration `accounts/0005` |
| Password reset signs the user out everywhere: refresh tokens are blacklisted, and access tokens carry a password fingerprint (`CHECK_REVOKE_TOKEN`) so they stop working at once | ✅ | `accounts/services.py`, `SIMPLE_JWT` |
| Account and invitation emails via Celery after commit; links built from `SITE_URL`, never the Host header | ✅ | `accounts/tasks.py`, `organizations/tasks.py` |
| Invitations never change an existing member's role, and each is used once (row lock) | ✅ | `organizations/services.py` |
| Django admin shows bookings and customers read-only: changes go through the services (tenant checks, locking, audit) | ✅ | `bookings/admin.py` |
| JWT rotation + blacklist | ✅ | `SIMPLE_JWT` |
| Tenant isolation: membership-backed tenant resolution, no superuser bypass | ✅ | `organizations/tenancy.py`, [MULTI_TENANCY.md](MULTI_TENANCY.md) |
| Capability-based authorization on every tenant endpoint | ✅ | `organizations/permissions.py`, [PERMISSIONS.md](PERMISSIONS.md) |
| One authorization model (memberships + capabilities); the legacy global `User.role` is removed | ✅ | M1.6 |
| Relation fields limited to the tenant's own rows; no `fields="__all__"` | ✅ | `core/api.py`, `core/test_api_contract.py` |
| Appointment changes only through the booking service, which itself refuses another organization's service, staff or customer | ✅ | `bookings/services.py` |
| Audit logging of important actions (append-only, scrubbed) | ✅ | `core/audit.py`, see below |
| Environment-based config; refuses the default `SECRET_KEY` when `DEBUG` is off | ✅ | `config/settings.py` |
| CSRF, clickjacking (`DENY`), `nosniff`, auto-escaping templates | ✅ | Django defaults and settings |
| `manage.py check --deploy` in CI | ❌ | Planned for M11.1 |
| HSTS, secure cookies and SSL redirect enforced in production | ❌ | Configurable through env vars today; enforced in M11.1 |
| Login and password-reset rate limiting | ✅ | Scoped throttles `login` / `password_reset` (`accounts/views.py`) |
| Account lockout after repeated failures | ❌ | M2.5 (web session login) |
| Forwarded headers trusted only behind a configured proxy (`TRUSTED_PROXY_COUNT`) | ✅ | `config/settings.py`, [DEPLOYMENT.md](DEPLOYMENT.md) |
| Double-booking prevention under concurrency (staff-calendar lock, PostgreSQL race tests) | ✅ | `bookings/services.py`, `tests/test_booking_engine.py` |
| Database-level exclusion constraint as a second guarantee | ❌ | M4.2 (see [BOOKING_ENGINE.md](BOOKING_ENGINE.md)) |
| Upload validation (size, type) | ❌ | M11.1 |
| Customer anonymization (customer record and the copies on appointments, notifications, waitlist) | ✅ | `crm/services.py`, [CRM.md](CRM.md); API endpoint in M2.4 |
| Customer data export | ❌ | M11.1 |

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
