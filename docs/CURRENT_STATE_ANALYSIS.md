# Current State Analysis

**Date:** 2026-09-26
**Snapshot:** `d3b31b84051c8c6da747bf3c83d0b24b8fd163c7` (branch `main`, 15 commits, last message "up to date")
**Scope:** Phase 0 assessment for turning this repo into a multi-tenant Booking + Appointment + CRM SaaS.
**Method:** Every Python module, migration, template and doc was read. The test suite was run, and a set of probe tests was run against a scratch copy (details in §1.3). No application code in the repository was changed.

> This document replaces `docs/EXISTING_PROJECT_AUDIT.md` (2026-07-19), which describes an older state of the code. That file is kept for history.

---

## 1. Snapshot and Health

### 1.1 The repository does not currently run

| Check | Result |
|---|---|
| `python manage.py check` | **Fails.** `SyntaxError` at `accounts/models.py:48` |
| `pytest` | **0 tests collected.** Same `SyntaxError` during Django setup |
| `makemigrations --check` | **Fails** once the conflicts are resolved: two leaf nodes in `accounts` |
| `ruff check .` | 315 findings: 166 E501, 86 invalid-syntax (conflict markers), 35 I001, 24 F401, others |
| CI (`.github/workflows/ci.yml`) | Would fail at the Ruff step |

**Root cause:** an unfinished `git stash pop` left conflict markers (`<<<<<<< Updated upstream` / `>>>>>>> Stashed changes`) in four tracked files:

| File | Upstream side | Stashed side |
|---|---|---|
| `accounts/models.py:48-65` | Global `User.role` field (`customer/specialist/admin/owner`) | Email-first user fields (`profile_image`, `email_verified`, `account_status`, `USERNAME_FIELD="email"`, `CustomUserManager`) |
| `accounts/serializers.py:70-86` | Profile fields with `username` | Read-only profile fields |
| `appointments/tests.py:25-33` | Username-style `create_user("user1", …)` | Email-style `create_user(email=…)` |
| `specialists/tests.py:31-35` | Same | Same |

Both sides were migrated, which is why there are two conflicting `accounts` migrations: `0002_user_role` (2026-08-28) and `0002_alter_user_managers_user_account_status_and_more` (2026-07-19).

### 1.2 Test results after resolving the conflicts in a scratch copy

To measure the real state of the suite, I resolved the conflicts in a throwaway copy:
- Models: kept both sides.
- Serializer: kept the stashed side.
- Tests: kept the stashed side, plus `role=ADMIN` on the admin user.
- Added a merge migration.

Python 3.14.3, SQLite, Django 6.0.7:

```
147 failed, 134 passed, 8 skipped   (7 min 12 s)
```

| Area | Passed | Failed | Notes |
|---|---|---|---|
| `appointments/test_scheduling.py`, `test_transactions.py` (legacy) | most | – | High-quality interval and lock tests |
| `appointments/tests.py` (legacy) | some | 124 | ~120 call the old signature `create_superuser("admin", email, pw)` → `TypeError` on the email-first manager |
| `specialists/tests.py` (legacy) | some | 15 | Same root cause |
| `accounts/test_role_permissions.py` | some | 8 | Tests written for the username model / global `User.role` |
| `bookings`, `organizations`, `accounts/tests.py` (new stack) | **6** | 0 | This is the **entire** test coverage of the multi-tenant stack |
| Skipped (8) | – | – | PostgreSQL-only concurrency tests in `appointments/test_transactions.py` (`skipUnless(connection.vendor == "postgresql")`) |

**Conclusion:** almost all the green tests exercise the **legacy, non-tenant** `appointments`/`specialists` apps. The multi-tenant stack (`organizations`, `bookings`, `scheduling`, `services`, `staff`, `subscriptions`, `notifications`, `dashboard`) has **six tests**, and none of them exercises an API endpoint.

### 1.3 Probe tests (scratch copy only, not committed)

I wrote a small `APITestCase` probe against the resolved copy to confirm the suspected isolation defects. Results:

| # | Probe | Result |
|---|---|---|
| P1 | **Anonymous** `GET /api/v1/waitlist/` with header `X-Organization-Slug: org-b` | **200**, returns org B's waitlist including `customer_name`, `customer_email`, `customer_phone` |
| P2 | A **customer-role** member of org A: `GET /api/dashboard/summary/` with `X-Organization-Slug: org-b` | **200**, returns org B's booking counts, revenue, customer and staff counts |
| P3 | A **customer-role** member: `PATCH /api/organizations/current/ {"name":"pwned","is_suspended":true}` | **200**. Organization renamed and suspended |
| P4 | Manager of A: `POST /api/v1/availability/weekly/` with org B's `staff` id | **201**. Row created in org A pointing at org B's staff |
| P5 | Manager of A: `PATCH /api/v1/services/{A}/ {"assigned_staff_members":[B staff]}` | **200**. Cross-tenant M2M link created |
| P6 | Booking customer: `PATCH /api/v1/bookings/{id}/ {"status":"completed","service":<org A service>}` | **200**. Status forced and service moved to another tenant |
| P7 | `POST /api/v1/bookings/` at 03:07 for staff with **zero** availability rows | **201**. The booking engine does not check availability |
| P8 | Manager invites a user with `role: "owner"` | 400, but only because `expires_at` is a required field. Nothing validates the role |
| P9 | Any booking creation with Celery pointed at a real broker | `kombu.EncodeError: Object of type Booking is not JSON serializable`. `.delay()` **always** fails and falls back to sending synchronously inside the booking transaction |
| P10 | Booking creation with `DEBUG=False` and no Redis running | The request blocks while Celery retries the broker connection |

---

## 2. Existing Architecture

A modular Django monolith ("Schedula") with **two overlapping generations of code**:

```
config/            settings (django-environ), urls, celery
┌──────────── Generation 2: multi-tenant (UUID PKs, org-scoped) ────────────┐
core/              BaseUUIDModel, AuditLog, request thread-local, health, landing pages, seed_demo
accounts/          email-first User, JWT auth flows, EmailVerificationToken, LoginHistory
organizations/     Organization, OrganizationMembership(role), OrganizationInvitation, selectors
services/          ServiceCategory, Service
staff/             StaffProfile
scheduling/        WeeklyAvailability, AvailabilityException, TimeOff, OrganizationHoliday, slot generator
bookings/          Customer, Booking, BookingStatusHistory, BookingActivityLog, WaitlistEntry, services, public web booking
notifications/     NotificationLog, send_templated_email task
subscriptions/     Plan, Subscription, enforce_plan_limit (never called)
dashboard/         summary selector + endpoint
api/               /api/v1/ router composition only
└────────────────────────────────────────────────────────────────────────────┘
┌──────────── Generation 1: legacy single-tenant (int PKs) ─────────────────┐
specialists/       Specialist, WorkingHour
appointments/      Appointment(date,time), ALLOWED_TRANSITIONS, scheduling.py, transactions.py
accounts           global User.role + accounts/permissions.py
└────────────────────────────────────────────────────────────────────────────┘
templates/         8 minimal templates (landing, features, pricing, public booking, success, 2 emails)
```

**Patterns in use:**
- `services.py` for writes, `selectors.py` for reads. Applied only partly: `bookings`, `organizations`, `dashboard` and `scheduling` have them; `services`, `staff` and `subscriptions` do not.
- `BaseUUIDModel` gives UUID PKs plus `created_at`/`updated_at`.
- DRF `ModelViewSet`s with `scope_queryset_by_organization()`.
- An `AuditLog` helper that reads the current request from a thread-local.

**Two role systems coexist:**
1. `accounts.User.role`: global, used only by legacy apps.
2. `OrganizationMembership.role` (`owner/manager/staff/customer`): per-tenant, used by the new stack.

**Stack:** Python 3.14, Django 6.0.7, DRF 3.17, SimpleJWT 5.5 (rotation + blacklist), drf-spectacular with the sidecar bundle, django-filter, Celery 5.5, Redis, psycopg 3, WhiteNoise, gunicorn, pytest-django, factory-boy (listed but unused), Ruff/Black/isort, pre-commit.

**Infrastructure:** `docker-compose.yml` defines `web`, `db` (postgres:17), `redis`, `celery`, `celery-beat`. `entrypoint.sh` waits for PG, migrates and runs `collectstatic` on **every** container start, including the worker and beat containers, which share the image's entrypoint. CI runs Ruff, Black, `check`, `makemigrations --check`, pytest with coverage, and a Docker build against PG17 and Redis.

---

## 3. Models

### 3.1 Generation 2 (multi-tenant)

| App | Model | Key fields | Indexes / constraints | Notes |
|---|---|---|---|---|
| core | `BaseUUIDModel` (abstract) | `id` UUID PK, `created_at`, `updated_at` | – | Good foundation |
| core | `AuditLog` | `organization?`, `user?`, `action`, `object_type`, `object_identifier`, `metadata` JSON, `ip_address`, `user_agent` | (action), (object_type, object_identifier) | No (organization, created_at) index. `ip` comes from `REMOTE_ADDR` only, so it is wrong behind a proxy |
| accounts | `User(AbstractUser)` | `email` unique, `phone_number`, **`role` (legacy)**, `profile_image`, `terms/privacy_accepted_at`, `email_verified`, `account_status` | – | Integer PK. `username` is still present and auto-generated |
| accounts | `EmailVerificationToken`, `LoginHistory` | token, expiry / ip, UA | token unique | LoginHistory records successful logins only |
| organizations | `Organization` | `public_uuid`, `name`, `slug` unique, `logo`, contact, `timezone`, `currency`, `booking_page_enabled`, `booking_page_theme` JSON, `default_appointment_rules` JSON, `allow_guest_booking`, `reminder_hours_before`, `second_reminder_hours_before`, `is_active`, **`is_suspended` (never enforced)** | slug unique | Branding and settings live in JSON blobs; reminder fields are unused |
| organizations | `OrganizationMembership` | org, user, `role` (owner/manager/staff/customer), `is_active` | unique(org,user); (org,role,is_active) | No receptionist role, no granular permissions |
| organizations | `OrganizationInvitation` | email, role, token, expiry, inviter, accepted_* | (org,email,expires_at) | – |
| services | `ServiceCategory` | org, name, slug | unique(org,slug) | – |
| services | `Service` | org, category, price, currency, duration, buffers before/after, `is_active/is_public/is_archived`, image, color, `max_advance_days`, `min_notice_minutes`, cancellation/rescheduling deadlines, `capacity`, M2M `assigned_staff_members` | unique(org,slug); (org,active,public); (org,archived) | Buffers and deadlines are **never used** by the engine. No locations |
| staff | `StaffProfile` | user, org, job_title, bio, image, phone, `is_active`, `is_accepting_bookings`, color | unique(org,user); (org,active,accepting) | No locations, no limits |
| scheduling | `WeeklyAvailability` | org, staff, day 0-6, start/end | check(day 0-6) | No location |
| scheduling | `AvailabilityException` | org, staff, date, all-day or start/end, reason | – | At most one per day is used |
| scheduling | `TimeOff` | org, staff, start/end datetime, `approval_status` | – | – |
| scheduling | `OrganizationHoliday` | org, date, name, full-day or partial | unique(org,date,name) | Partial closures are ignored by the slot generator |
| bookings | `Customer` | org, `user?`, **`name`** (single field), `email` (required), `phone`, `notes` text, **`tags` JSON list**, `total_bookings`, `no_show_count`, `last_appointment` | unique(org,email); (org,email) | Counters are never updated. A customer without an email is impossible |
| bookings | `Booking` | `public_uuid`, `reference` "SCH-YYYY-NNNNNN", org, customer, denormalized customer name/email/phone, service, staff, start/end, customer and org timezone, `status` (8 values incl. `rejected`), `payment_status`, price and duration snapshots, customer/internal notes, cancellation fields, `rescheduled_from` | (org,start,status); (org,customer_email); (org,staff,start) | **No overlap constraint.** No location, `booking_source` or `created_by` |
| bookings | `BookingStatusHistory` | booking, old/new status, changed_by, note | – | Written on create/cancel only; `update_status` bypasses it |
| bookings | `BookingActivityLog` | booking, org, actor, action, metadata | – | Overlaps with `AuditLog` |
| bookings | `WaitlistEntry` | org, service, preferred_staff, date range, customer name/email/phone, status | – | Not linked to `Customer`. No location or time-of-day preference |
| notifications | `NotificationLog` | org, recipient, email, type, channel (`email` only), status, related_booking, sent_at, failure_reason, retry_count | (org,type,status) | No dedupe key, no templates model |
| subscriptions | `Plan` | name, slug, prices, `maximum_staff/services/monthly_bookings`, `analytics_enabled`, `api_access_enabled` | slug unique | Limits are hard-coded columns, not entitlements |
| subscriptions | `Subscription` | OneToOne org, plan, status, cycle, trial/period dates, `external_*_id` | – | No billing provider abstraction |

### 3.2 Generation 1 (legacy, not tenant-aware)

| App | Model | Notes |
|---|---|---|
| specialists | `Specialist` (int PK, OneToOne user, name, profession, `slot_duration`) and `WorkingHour` (day, start/end, unique) | Duplicates `StaffProfile` and `WeeklyAvailability` without an organization |
| appointments | `Appointment` (int PK, user, specialist, naive `date` + `time`, status, notes, duration) | Partial unique constraint on (specialist,date,time) for active statuses. Duplicates `Booking` without an organization or timezone |

### 3.3 Migrations
16 migration files, all additive. The only conflict is the double `accounts/0002`. There are no data migrations. The legacy and new tables coexist in one schema.

---

## 4. Existing Features

| Feature | State |
|---|---|
| Email-first registration, JWT login/refresh/logout (blacklist), email verification, password reset | API only. Works after the conflicts are resolved. No web login pages |
| Organizations, memberships, invitations (create, accept with email match) | API only. Weak authorization (§8) |
| Service catalogue with categories | API CRUD, manager/owner only |
| Staff profiles | API CRUD, manager/owner only |
| Weekly availability, exceptions, time off, holidays | API CRUD |
| Slot generation for a single staff member on a single date | `scheduling/services.py::generate_slots`. Ignores buffers and partial holidays, runs N+1 queries per slot, no "any provider" option |
| Booking create / cancel / reschedule / update_status | API. Correctness gaps (§10) |
| Public booking page `/book/<slug>/` | One HTML form where the customer types an ISO datetime by hand and picks staff by **email address**. No slot picker |
| Waitlist | Model plus API CRUD only. No matching or notification |
| Email notifications | Confirmation and cancellation templates. The Celery path is broken (P9) |
| Dashboard | One JSON summary endpoint |
| Subscriptions | Models plus `enforce_plan_limit()`, which is never called |
| Audit log | Written for booking.created, invitation events and email verification |
| Health `/health/`, readiness `/ready/` | Working |
| OpenAPI `/api/schema/`, Swagger `/api/docs/`, ReDoc `/api/redoc/` | Configured |
| `seed_demo` | One org "Demo Clinic", 5 users, 3 plans, 1 service, 1 booking. **Not idempotent**: a second run collides with its own booking and raises `ValueError`. Emails are sent while seeding |
| Legacy appointments/specialists API | Fully featured single-tenant API with the best tests in the repo |

---

## 5. APIs (current inventory)

**Mounted at `/api/` (unversioned):**
- accounts: `register/`, `login/`, `logout/`, `verify-email/`, `resend-verification/`, `forgot-password/`, `reset-password/`, `profile/`, `token/refresh/`
- organizations: `organizations/current/` (GET/PATCH), `organizations/memberships/`, `organizations/invitations/`, `organizations/invitations/accept/`
- dashboard: `dashboard/summary/`
- legacy: `specialists/…` (6 routes), `appointments/…`, `my-appointments/` (7 routes)

**Mounted at `/api/v1/` (router):**
- `services`, `service-categories`, `staff`
- `availability/weekly`, `availability/exceptions`, `availability/time-off`, `availability/holidays`, `availability/slots/available-slots`
- `bookings` (+ `cancel`, `reschedule`, `update_status`), `customers`, `waitlist`

`docs/API.md` claims the base path is `/api/v1/`, but half of the endpoints actually live under `/api/`. The legacy `/api/appointments/` and new `/api/v1/bookings/` both expose booking operations.

**Serializer conventions:**
- Many serializers use `fields="__all__"` with writable FK fields, and none validates that a related object belongs to the request's organization (P4, P5, P6).
- `organization` is read-only everywhere, which is good. It is set from `get_request_organization()`.

## 6. UI (current inventory)

| Route | Template | State |
|---|---|---|
| `/` | `web/landing.html` | 12-line placeholder |
| `/features/`, `/pricing/` | placeholders | Static text |
| `/book/<slug>/` | `web/public_booking.html` | Plain form (see §4) |
| `/book/success/<reference>/` | `web/booking_success.html` | Shows the reference from the URL |
| `/admin/` | Django admin | The only management UI that exists |

`base.html` loads the **Tailwind Play CDN** (`cdn.tailwindcss.com`, which is not meant for production), plus unpinned Chart.js and HTMX/Alpine from CDNs. Nothing uses HTMX or Alpine yet.

There is **no** login page, organization app shell, dashboard, calendar, CRM, staff or reception UI, customer portal, or SaaS admin UI. For practical purposes, the whole of the brief's UI work (§13, §21-§29, §52-§53) is greenfield.

## 7. Permissions and Roles

- **DRF defaults:** `IsAuthenticated`; JWT and Session auth; anon throttle 100/h and user throttle 1000/h. The `login` and `password_reset` scopes are defined but **no view uses them**.
- **`core/permissions.py`:** `IsPlatformAdmin` (is_superuser), `IsOrganizationMember`, `IsOrganizationManagerOrOwner`. All three resolve the organization through `get_request_organization()`.
- **`organizations/selectors.py::user_has_org_role`:** a superuser passes every check.
- **Legacy:** `accounts/permissions.py` (global `User.role`), `appointments/permissions.py`, `specialists/permissions.py`.
- **Missing:**
  - A receptionist role.
  - Capability-based permissions (`appointments.manage`, etc.).
  - Object-level rules for staff ("own appointments").
  - A platform-staff role distinct from Django `is_superuser`.
  - Enforcement of `Organization.is_suspended`.

## 8. Tenancy Mechanism and Isolation Risks

**Mechanism.** `get_request_organization(request)`:
1. Reads `X-Organization-Slug` header or `?organization=` → `Organization.objects.get(slug=…, is_active=True)`. **No membership check at this step.**
2. Otherwise uses the **first** active membership of the user (the order is undefined).

`scope_queryset_by_organization(qs, request)` filters by that organization. Superusers get **unfiltered** querysets across all tenants.

Isolation therefore holds only where the view also applies a membership permission. It fails wherever a view doesn't.

| # | Risk | Severity | Evidence |
|---|---|---|---|
| T1 | `WaitlistViewSet` uses `IsAuthenticatedOrReadOnly`, so **anonymous users read any tenant's waitlist PII**. Any authenticated user can also create, update or delete entries in any tenant | **Critical** | P1, `bookings/views.py` |
| T2 | `OrganizationDashboardSummaryView` uses `IsAuthenticated` only, so **any user reads any tenant's metrics** | **Critical** | P2 |
| T3 | `CurrentOrganizationView` lets any member, including the `customer` role, edit the org, including `is_active`/`is_suspended` | **Critical** | P3 |
| T4 | Writable FK and M2M fields are not validated against the tenant (`staff`, `service`, `category`, `assigned_staff_members`, `preferred_staff`, `user` on StaffProfile), so cross-tenant references can be created | **High** | P4, P5 |
| T5 | `BookingViewSet` is a full `ModelViewSet`. The customer who owns a booking can PUT/PATCH/DELETE it, including `status`, `staff`, `service` and times, bypassing the booking engine | **High** | P6 |
| T6 | A tenant chosen from a client-supplied header is the core pattern. Safety depends on each view's permission class; one forgotten class leaks | **High** (design) | T1, T2 |
| T7 | The "first membership" fallback is non-deterministic for users in several orgs | Medium | selectors |
| T8 | A superuser gets **all tenants' data** from ordinary tenant endpoints: no explicit platform context, no audit | Medium | `scope_queryset_by_organization` |
| T9 | `update_status` has no transition rules or history. A manager can set any status | Medium | `bookings/views.py` |
| T10 | `SlotViewSet` (AllowAny) exposes non-public services and staff by id, and returns 500 on unknown ids (`DoesNotExist`) | Medium | `scheduling/views.py` |
| T11 | Celery tasks receive model instances, not ids. There is no tenant context contract for tasks | Medium | P9 |
| T12 | Legacy `/api/appointments/` and `/api/specialists/` are tenant-less and publicly routed. Legacy "admin" is a global role | Medium | `config/urls.py` |
| T13 | `OrganizationMembershipSerializer` exposes a writable `organization` (list view only today) | Low | serializer |
| T14 | Invitations don't validate that the inviter may grant the role (manager → owner) | Medium | P8 |
| T15 | Suspended orgs keep full access. `is_suspended` is never checked | Medium | grep |

## 9. Subscriptions

- `Plan` has columns for three hard-coded limits plus two booleans. `Subscription` is one-to-one with the org and has Stripe-shaped `external_*_id` columns.
- `enforce_plan_limit(org, "staff"|"services"|"bookings")` exists but is **never called**. Its "monthly bookings" check counts **all-time** bookings.
- There are no entitlements/features table, no usage tracking, no billing provider abstraction and no Stripe code. `.env.example` has `STRIPE_*` variables that nothing reads.
- `seed_demo` creates Free / Professional / Business plans.

## 10. Booking Engine and Concurrency

**`bookings/services.py::create_booking` (`@transaction.atomic`):**
1. Rejects times in the past.
2. Computes the end time from the duration only. **Buffers are ignored.**
3. Runs `Booking.objects.select_for_update().filter(overlap…).exists()`. **This does not protect an empty slot.** When no conflicting row exists, nothing is locked, so two concurrent transactions both see "no conflict" and both insert (the classic phantom/write-skew problem). On SQLite, `select_for_update` is a no-op. **The README's "concurrency-safe" claim is not true for the new stack.**
4. Does **not** validate:
   - weekly availability, exceptions, time off or holidays (P7);
   - `min_notice_minutes` or `max_advance_days`;
   - whether the staff member is assigned to the service;
   - `service.is_active`/`is_public` (the web path);
   - plan limits.
5. `get_or_create`s the `Customer` by email. Name and phone are not updated for existing customers.
6. Generates the reference with `random.choices` and a retry loop. There is no unique-violation retry.
7. Writes `BookingStatusHistory`, `BookingActivityLog` and `AuditLog`, then **queues the email inside the transaction** (P9, P10).
8. Always sets `status=CONFIRMED`. There are no pending/approval workflows.

**Other operations:**
- **`cancel_booking`:** no cancellation-deadline check, no audit log, no terminal-status guard (for example, a completed booking can be cancelled).
- **`BookingViewSet.reschedule`:** **not atomic**. It cancels the old booking and saves, then calls `create_booking`. If that raises (a conflict, or a time in the past), the old booking stays **cancelled**, and the `ValueError` surfaces as **HTTP 500**. There is no deadline check, no history on the old booking, and the new booking's `customer_user` is set to whoever made the request, which may be staff.
- **Error mapping:** service-layer `ValueError`s are not converted to 400/409 anywhere, so the API returns 500s.
- **`generate_slots`:** 15-minute step, per-slot `TimeOff`/overlap queries (N+1), only the first exception per day, ignores partial holidays and buffers. The date is interpreted in the org timezone but bookings are filtered by `start_datetime__date=date` in UTC, so the two disagree for non-UTC orgs.

**Legacy engine (worth reusing):**
- `appointments/transactions.py` locks the **parent row** (`Specialist`) with `select_for_update` before checking occupancy. That correctly serializes writers even when the slot is empty.
- It maps IntegrityError/serialization failures to a controlled 400.
- It has PostgreSQL race tests using `pg_blocking_pids`.
- `appointments/scheduling.py` has well-tested half-open interval logic.

This is the right approach to port to `Booking`, locking `StaffProfile` (and later resources), plus a PostgreSQL exclusion constraint as the final guarantee.

## 11. Notifications and Celery

- `queue_booking_notification` creates a `NotificationLog` and then:
  - in `DEBUG`, calls `send_templated_email.apply()` synchronously;
  - otherwise calls `.delay()`, which **always raises** `EncodeError`, because the payload includes a `Booking` instance and the serializer is JSON. The `except` then runs the task synchronously.
  - **Net effect: emails are always sent synchronously inside the booking transaction.**
- When no broker is reachable, `.delay()` blocks while it retries (P10).
- `send_templated_email` retries 3 times. On failure it marks the log `FAILED` before the retry.
- **No beat schedule is defined.** There is no reminder task at all. `Organization.reminder_hours_before` and `second_reminder_hours_before` are unused.
- Other gaps:
  - Emails are hard-coded to `emails/*.txt|html`, with no organization-level templates and no SMS abstraction.
  - The invitation, verification and password-reset emails are sent synchronously from views with `send_mail`.

## 12. Technical Debt

1. Conflict markers and the unresolved migration fork (§1.1). **Blocker.**
2. Two parallel domains for the same concepts: `Specialist`/`Appointment` versus `StaffProfile`/`Booking`, plus two role systems.
3. Stray committed files:
   - `command2-makemigrations.txt` … `command5-test.txt` (UTF-16 PowerShell logs from another machine).
   - `requiremnts.txt` (a misspelled duplicate).
4. The `Makefile` hard-codes `c:/Users/Erexzen/.../python.exe`.
5. `pyproject.toml` `addopts = --maxfail=1` hides the extent of failures.
6. `STATICFILES_STORAGE` has been removed in Django 5.1+, so on Django 6 the setting is ignored and WhiteNoise compression is not active. Use `STORAGES`.
7. `SECRET_KEY` silently falls back to an insecure default when `DEBUG=False`.
8. Mixed indentation (tabs in some modules, spaces in others). 166 E501s.
9. `factory-boy` and `Faker` are listed but unused. There are no test factories or fixtures.
10. `Booking.generate_reference` defaults the year to a hard-coded 2026.
11. Customer aggregate counters (`total_bookings`, `no_show_count`, `last_appointment`) are declared but never maintained.
12. `BookingActivityLog` and `AuditLog` overlap.
13. `entrypoint.sh` runs migrate and collectstatic in every container, including celery and beat.
14. Docs overstate the implementation: README "concurrency-safe", SECURITY.md "tenant-scoped querysets" checked, API.md base path.
15. The public booking form shows staff **email addresses** to anonymous visitors (a PII leak) and makes the customer type an ISO datetime by hand.
16. Service-layer errors are raised as bare `ValueError`s. There is no domain exception hierarchy.
17. Front-end assets are loaded from CDNs, and Tailwind uses the Play CDN, so no production CSS build exists and a CSP can't be applied.

## 13. Security Observations

| Area | Observation |
|---|---|
| Tenant isolation | See §8. Critical findings T1–T3 |
| AuthN | Throttling is not applied to login, register, forgot-password or resend-verification (scopes defined but unused). There is no lockout. LoginHistory logs successes only |
| Password reset | `uid` is the raw integer PK. Django's token generator is used correctly. Emails are sent synchronously |
| Settings | No `SECURE_HSTS_*`. `DEBUG=True` in `.env.example`. Insecure SECRET_KEY fallback. `SECURE_PROXY_SSL_HEADER` is set unconditionally, which is fine only behind a trusted proxy |
| Audit | Covers few actions. IP comes from `REMOTE_ADDR`. The thread-local request is never cleared, a minor risk of stale context |
| Public endpoints | `/book/<slug>/` POST has no throttling or bot protection. Missing name/email → IntegrityError 500. A malformed UUID → 500 |
| Uploads | `ImageField` for logos and avatars with no size limits or content validation. `MEDIA` is served only when `DEBUG` |
| Output | Django templates auto-escape. There is no CSP; the CDN scripts would need allow-listing |
| Secrets | Read from the environment. `.env` is git-ignored |
| Clickjacking / CSRF / nosniff | `X_FRAME_OPTIONS=DENY`, CSRF middleware and `SECURE_CONTENT_TYPE_NOSNIFF` are all enabled |

## 14. Requirements Matrix (brief §5–§47)

Legend: ✅ Exists · 🟡 Partial · ❌ Missing

| § | Requirement | State | Where / notes |
|---|---|---|---|
| 3.1 | Multi-tenant Organization boundary | 🟡 | `organizations/models.py`. Models are org-scoped, but enforcement is broken (§8) |
| 4A | SaaS super admin UI | ❌ | Django admin only. `IsPlatformAdmin` exists (`core/permissions.py`) |
| 4B–E | Owner / Manager / Staff / Receptionist experiences | ❌ | Roles exist in `OrganizationMembership` except receptionist. No UI, no granular permissions |
| 4F | Customer portal | ❌ | Only a `my-appointments` legacy endpoint |
| 4G | Public booking | 🟡 | `bookings/web_views.py`. A raw form, not a wizard |
| 5 | CRM Customer entity | 🟡 | `bookings.Customer`: name, email, phone, notes, JSON tags. Most fields missing |
| 6 | 360° customer profile | ❌ | – |
| 7 | Tags | 🟡 | JSON list on Customer. No org-defined tag model |
| 8 | Activity timeline | 🟡 | `BookingActivityLog` (booking-scoped) and `AuditLog`. No customer timeline |
| 9 | Locations | ❌ | Only `Organization.address` and `timezone` |
| 10 | Services + categories | 🟡 | `services/models.py` has most fields. Missing `online_bookable` (≈`is_public`), tax, locations, required staff type |
| 11 | Staff management | 🟡 | `staff/models.py`. No locations, limits or per-location services |
| 12 | Scheduling engine | 🟡 | `scheduling/services.py`. Weekly, exceptions, time off, full-day holidays, notice/advance. Missing buffers, location hours, partial holidays, any-provider, TZ-correct day filter, **concurrency guarantee** |
| 13 | Calendar UI | ❌ | – |
| 14 | Lifecycle + status history | 🟡 | `Booking.Status` has all the requested statuses plus `rejected`. `BookingStatusHistory` exists. No transition rules (legacy `ALLOWED_TRANSITIONS` is reusable) |
| 15 | Appointment details | 🟡 | Missing `location`, `booking_source`, `created_by` |
| 16 | Waitlist | 🟡 | `WaitlistEntry`. No location, time preference, customer link or matching |
| 17 | Async notifications | 🟡 | NotificationLog + task, but broken dispatch (§11). Email only |
| 18 | Reminder engine | ❌ | Org fields exist, no task or beat schedule |
| 19 | Customer forms | ❌ | – |
| 20 | Global search | 🟡 | DRF `SearchFilter` on customers and staff only |
| 21 | Organization dashboard | 🟡 | `dashboard/selectors.py` JSON only. The "revenue" figure is shown without context |
| 22–24 | Staff / reception / portal dashboards | ❌ | – |
| 25 | SaaS admin UI | ❌ | – |
| 26 | Org admin UI | ❌ | – |
| 27 | Design system | ❌ | Tailwind CDN only |
| 28 | Branding | 🟡 | `logo`, `booking_page_theme` JSON, contact fields, slug URL |
| 29 | Public booking wizard | ❌ | See 4G |
| 30 | Plans / entitlements / usage | 🟡 | `subscriptions/models.py`. Column limits, no entitlements, no usage, not enforced |
| 31 | Billing abstraction / Stripe | ❌ | `external_*_id` fields only |
| 32 | Versioned API + OpenAPI | 🟡 | `/api/v1/` exists but is split with `/api/`. Swagger and ReDoc work |
| 33 | AI-agent-safe API | ❌ | No service accounts or API keys, no idempotency, no `booking_source` |
| 34 | Audit logging | 🟡 | `core/models.py::AuditLog` + `core/services.py::write_audit_log`. Few call sites |
| 35 | Security baseline | 🟡 | §13 |
| 36 | Privacy architecture | ❌ | Consent timestamps on User only. No export or anonymization |
| 37 | PostgreSQL, UUIDs, indexes | 🟡 | PG primary, UUID PKs in the new stack, several good indexes. User has an integer PK |
| 38 | Domain service layer | 🟡 | `bookings/services.py`, `organizations/services.py`. Views still contain logic (reschedule, update_status) |
| 39 | Selectors | 🟡 | `organizations/selectors.py`, `dashboard/selectors.py` |
| 40 | Celery jobs with tenant context | 🟡 | Worker + beat configured. No scheduled jobs. Tasks take model objects |
| 41 | Test strategy | 🟡 | pytest configured. The new stack is essentially untested |
| 42 | Concurrency tests | 🟡 | Legacy only (PostgreSQL). None for `Booking` |
| 43 | Demo data | 🟡 | `core/management/commands/seed_demo.py`. One org, not idempotent |
| 44 | Docker dev | ✅ | `docker-compose.yml` has all 5 services (the entrypoint needs fixing) |
| 45 | `.env.example` | ✅ | Complete. Stripe variables are present but unused |
| 46 | Deployment readiness | 🟡 | `docs/DEPLOYMENT.md`, health/ready, gunicorn. No object storage, no proxy docs |
| 47 | Docs set | 🟡 | ARCHITECTURE, BOOKING_ENGINE, API, DEPLOYMENT, SECURITY exist. DATABASE, MULTI_TENANCY, PERMISSIONS, CRM, DEVELOPMENT, SAAS_ADMIN and SUBSCRIPTIONS are missing |

## 15. Reusable Components

| Component | Reuse as |
|---|---|
| `core.models.BaseUUIDModel` | Base for all new tenant models |
| `core.models.AuditLog` + `core.services.write_audit_log` | Audit framework (extend: action enum, proxy-aware IP, org index) |
| `accounts` email-first User, JWT flows, verification, reset | Auth foundation (drop legacy `role` later) |
| `organizations.Organization/Membership/Invitation` + `services.accept_invitation` | Tenant and membership core (extend roles and permissions) |
| `services.Service/ServiceCategory` | Service catalogue (add locations and online flags) |
| `staff.StaffProfile` | Provider profile (add locations and limits) |
| `scheduling.*` models | Availability model (add location) |
| `scheduling.services.generate_slots` | Starting point for `AvailabilityService`. Rewrite internals for batching, buffers and any-provider |
| `bookings.Booking`, `BookingStatusHistory`, `WaitlistEntry` | Appointment core. Keep the table and model name `Booking`; label it "Appointment" in the UI |
| `bookings.Customer` | Becomes the CRM customer (extend in place) |
| `bookings.services.create_booking/cancel_booking` | Refactor into `BookingService` (same entry point for web, API and AI) |
| `appointments.transactions` (parent-row lock, conflict mapping) and `ALLOWED_TRANSITIONS` | Port to `Booking` |
| `appointments/test_transactions.py` PG race harness, `test_scheduling.py` interval cases | Port to `Booking` tests |
| `notifications.NotificationLog` + `send_templated_email` | Notification pipeline (fix the payload, dispatch on commit) |
| `subscriptions.Plan/Subscription` | Keep. Add an entitlements layer |
| `dashboard.selectors` | Seed of `ReportingService` |
| Docker Compose, CI workflow, pre-commit, Ruff/Black config | Keep |
| drf-spectacular + sidecar | Keep |

## 16. Required Database Changes (summary)

| Change | Kind | Risk |
|---|---|---|
| Merge `accounts/0002_*` fork | Merge migration | Low |
| `OrganizationMembership`: add `receptionist` role choice, per-membership permission overrides | Additive | Low |
| `Organization`: add `status`/suspension metadata, branding fields (primary color, booking instructions) | Additive | Low |
| `AuditLog`: add index (organization, created_at) | Additive | Low |
| New `locations.Location` (+ business hours); `Booking.location`, `WeeklyAvailability.location`, `StaffProfile.locations` M2M, `Service.locations` M2M | Additive, nullable → backfill default "Main" location per org → then NOT NULL for Booking | **Medium** (data migration) |
| `Customer`: add first/last/preferred name, phones, birthday, address fields, language, contact preference, source, status, consent flags, assigned/preferred staff, `created_by`; split `name` → first/last | Additive + data migration | Medium |
| New `crm.Tag` + Customer↔Tag M2M; migrate the JSON `tags` list → Tag rows | Additive + data migration | Medium |
| New `crm.CustomerNote`, `crm.CustomerActivity` | Additive | Low |
| `Booking`: add `booking_source`, `created_by`, `location`; status transition rules (code only) | Additive | Low |
| `Booking`: PostgreSQL **exclusion constraint** (staff, tstzrange incl. buffers) for active statuses; requires `btree_gist` | Constraint; must first verify no existing overlaps | **Medium–High** |
| `WaitlistEntry`: add `customer` FK, `location`, time-of-day preference | Additive | Low |
| `NotificationLog`: add `channel` values, `dedupe_key` unique, `scheduled_for`; new `NotificationTemplate` | Additive | Low |
| New `forms` app (FormTemplate, FormQuestion, FormAssignment, FormSubmission, FormAnswer) | Additive | Low |
| `subscriptions`: new `Feature`, `PlanFeature` (entitlement/limit), `OrganizationUsage`; migrate the Plan columns into rows | Additive + data migration | Medium |
| Legacy `specialists`/`appointments`: freeze, unroute, eventually drop (explicitly approved, with a backup/export step) | Destructive, **deferred** | High; needs a decision |
| `User.role`: deprecate (stop reading), drop later | Destructive, deferred | Medium |

## 17. Required UI Changes (summary)

Almost all of the UI is new work:
- **Foundation:**
  - A build pipeline (Tailwind standalone CLI in the Docker build).
  - Vendored HTMX, Alpine and FullCalendar static files.
  - A component partials library.
  - Session login, logout and password reset pages.
  - Role-based home routing.
- **Areas:**
  - `/saas/…` SaaS admin shell.
  - `/app/…` organization shell with a permission-driven sidebar: dashboard, calendar, appointments, customers (list + 360° profile), staff, services, locations, forms, communications, reports, settings, subscription.
  - Reception dashboard.
  - `/staff/…` provider area.
  - `/portal/…` customer portal.
  - `/book/<slug>/` an 8-step HTMX booking wizard, replacing the current form.
- **Emails:** organization-branded templates.
