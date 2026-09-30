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
| Django admin shows organizations, memberships and invitations read-only (invitation tokens hidden); suspend and reactivate are audited admin actions | ✅ | `organizations/admin.py` |
| `seed_demo` (published demo passwords, a platform superuser) refuses to run unless `DEBUG` is on or `--allow-without-debug` is passed | ✅ | `core/management/commands/seed_demo.py` |
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
| "Forgot password" and "resend verification" do the same work for every address: the request queues one task keyed by the email and the worker looks the account up, so neither the answer nor its timing reveals which addresses have accounts | ✅ | `accounts/services.py`, `accounts/tasks.py` |
| Login and password-reset rate limiting | ✅ | API: scoped throttles `login` / `password_reset` (`accounts/views.py`). Web: per client IP **and** per email (`WEB_LOGIN_RATE`, `WEB_PASSWORD_RESET_RATE`; `core/ratelimit.py`) |
| Account lockout after repeated failures | ✅ (temporary) | Too many attempts on one email pause sign-in for that email for the rate window (default 10 per 15 minutes). There is no permanent lockout, which anyone could trigger against someone else's account |
| Content Security Policy: only self-hosted scripts and styles, nothing inline, no `eval` (Alpine CSP build, htmx without eval) | ✅ | `SECURE_CSP` in `config/settings.py`; the API docs pages have their own looser policy (`core/csp.py`) |
| No third-party scripts: htmx and Alpine vendored and checked against the npm registry's integrity hashes; Tailwind CLI pinned by SHA-256 | ✅ | `static/vendor/`, `core/management/commands/tailwind.py` |
| Web pages enforce the same capabilities as the API; a hidden menu link is never the only protection (tested page by page) | ✅ | `core/web.py`, `core/navigation.py`, `tests/test_web_shell.py` |
| Sign-out is POST only; `next` redirects are limited to this site | ✅ | `accounts/web_views.py`, `organizations/web_views.py` |
| CSRF cookie is HttpOnly; htmx sends the token from the page | ✅ | `CSRF_COOKIE_HTTPONLY`, `templates/base.html` |
| A password-reset link works once even under concurrent use: the account row is locked and the token re-checked under the lock (PostgreSQL race test) | ✅ | `accounts/services.py` (`reset_password_with_token`), `tests/test_review_3_findings.py` |
| Emailed links need a click to act (email verification is a POST), so link scanners can't verify for the user | ✅ | `accounts/web_views.py` |
| htmx keeps no page snapshots in `localStorage` (`historyCacheSize: 0`) | ✅ | `templates/base.html` |
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
