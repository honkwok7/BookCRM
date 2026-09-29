# Implementation Plan: Booking + Appointment + CRM SaaS

**Date:** 2026-09-26
**Based on:** `docs/CURRENT_STATE_ANALYSIS.md` (snapshot `d3b31b8`)
**Replaces:** the 2026-07-19 plan previously in this file. Its phases 1–10 were largely carried out as scaffolding. It is still available in git history (`git show d3b31b8:docs/IMPLEMENTATION_PLAN.md`).

---

## Guiding decisions

1. **Keep the modular Django monolith.** Extend the Generation-2 apps (`organizations`, `bookings`, `scheduling`, `services`, `staff`, …). Do not rewrite them.
2. **Keep the model name `Booking`; call it "Appointment" in the UI and docs.** Renaming the table and model would touch every FK, migration and serializer for no functional gain. Resource names in API v1 stay `bookings` (with an `appointments` alias considered in M10.1).
3. **Extend `bookings.Customer` in place as the CRM customer.** Put the new CRM models (Tag, Note, Activity) in a new `crm` app that references `bookings.Customer`. Moving `Customer` across apps is not worth the migration risk now.
4. **Tenant = `Organization`, resolved server-side from membership.** A client-supplied slug may only *select among the user's own memberships*. It never grants access on its own. Public pages resolve the tenant from the URL slug only, with a read-only public scope.
5. **Permissions come from capabilities, not roles.** A code-defined capability registry (`appointments.manage`, …) maps from roles (`owner`, `manager`, `receptionist`, `staff`, `customer`) with per-membership grant and revoke overrides. Views check capabilities.
6. **One booking engine.** `BookingService` and `AvailabilityService` are the only write and read paths used by web views, the API, the admin, `seed_demo` and future AI agents.
7. **Double-booking protection has two layers:**
   - (a) `select_for_update()` on the `StaffProfile` row (the parent-row lock pattern ported from `appointments/transactions.py`), then re-validation;
   - (b) a PostgreSQL `EXCLUDE USING gist (staff_id WITH =, tstzrange(blocked_start, blocked_end) WITH &&) WHERE status IN (active)` constraint as the final guarantee. A violation maps to **HTTP 409**.
8. **PostgreSQL is the reference database, for production and for tests** (confirmed). SQLite remains only as a best-effort fallback for quick local runs. Concurrency and constraint tests are marked `@pytest.mark.postgres` and must pass in CI, in Docker and on developer machines with a local PostgreSQL.
9. **Side effects happen after commit.** Notifications use `transaction.on_commit`, and Celery payloads carry **ids plus `organization_id`** only.
10. **Front end:** Django templates + HTMX + Alpine + Tailwind, built with the Tailwind **standalone CLI** (no Node or Python dependency) in the Docker build. HTMX, Alpine and FullCalendar are vendored under `static/vendor/`. No React.
11. **Legacy `specialists`/`appointments`:** freeze now (M0). Port their good ideas (locking, transition table, interval tests) into the Generation-2 stack, then remove the apps and drop their tables in M1.6. This was approved on 2026-09-27; there is no production data.

## Migration strategy (applies to every schema milestone)

1. **Additive first:** new nullable columns and new tables only.
2. **Backfill** in a separate data migration (`RunPython` with a reverse function, batched with `iterator()`). It must be idempotent.
3. **Constrain** (NOT NULL, unique, exclusion) in a third migration, only after a pre-check command reports zero violations (for example `manage.py check_booking_overlaps`).
4. **Never drop** a column or table in the same release that stops using it. Drops are separate, explicitly approved milestones.
5. PostgreSQL-only DDL (`btree_gist`, `ExclusionConstraint`) goes in migrations guarded by `connection.vendor == "postgresql"` (conditional `RunSQL`/`RunPython`), so SQLite still migrates.
6. Every milestone's migrations must pass both `makemigrations --check` and `migrate` then `migrate <app> <previous>` (reversibility) in CI.

## Target route map

| Area | Routes | Guard |
|---|---|---|
| Marketing | `/`, `/features/`, `/pricing/` | public |
| Auth | `/accounts/login/`, `/accounts/logout/`, `/accounts/password-reset/…`, `/accounts/register/`, `/accounts/verify-email/`, `/invitations/accept/` | public / session |
| SaaS admin | `/saas/`, `/saas/organizations/…`, `/saas/subscriptions/`, `/saas/plans/`, `/saas/users/`, `/saas/features/`, `/saas/audit/`, `/saas/system/` | `is_platform_staff` |
| Organization app | `/app/dashboard/`, `/app/calendar/`, `/app/appointments/…`, `/app/customers/…`, `/app/staff/…`, `/app/services/…`, `/app/locations/…`, `/app/forms/…`, `/app/communications/…`, `/app/reports/`, `/app/settings/…`, `/app/subscription/`, `/app/reception/`, `/app/waitlist/`, `/app/switch/<org>/` | membership + capability |
| Provider | `/staff/dashboard/`, `/staff/calendar/`, `/staff/customers/`, `/staff/availability/` | membership `staff` + own-scope |
| Customer portal | `/portal/`, `/portal/book/`, `/portal/appointments/…`, `/portal/forms/…`, `/portal/profile/` | authenticated customer (per-org Customer link) |
| Public booking | `/book/<org-slug>/` (HTMX wizard), `/book/<org-slug>/confirmation/<public_uuid>/` | public, throttled |
| API | `/api/v1/…` (all resources); `/api/schema/`, `/api/docs/`, `/api/redoc/`; legacy `/api/*` aliases deprecated in M10.1 | JWT / session / API key |
| Ops | `/health/`, `/ready/`, `/admin/` (technical only) | – |

## Draft permission matrix (implemented as capabilities in M1.2)

| Capability | SaaS Admin | Owner | Manager | Reception | Staff | Customer |
|---|---|---|---|---|---|---|
| `organization.manage` (profile, branding, booking policies) | Platform (via /saas) | ✅ | opt | ❌ | ❌ | ❌ |
| `billing.manage` / `billing.view` | Platform | ✅ / ✅ | ❌ / opt | ❌ | ❌ | ❌ |
| `staff.manage` | ❌ | ✅ | opt (default ✅) | ❌ | ❌ | ❌ |
| `services.manage`, `locations.manage` | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `appointments.view_all` | ❌ | ✅ | ✅ | ✅ | own | own |
| `appointments.manage` (create/reschedule/cancel/status) | ❌ | ✅ | ✅ | ✅ | own | own (policy-bound) |
| `customers.view` / `customers.manage` | ❌ | ✅ | ✅ | ✅ | assigned / ❌ | self |
| `customers.notes.private` | ❌ | ✅ | ✅ | opt | own notes | ❌ |
| `waitlist.manage` | ❌ | ✅ | ✅ | ✅ | ❌ | self |
| `reports.view` | Platform aggregates | ✅ | opt (default ✅) | ❌ | ❌ | ❌ |
| `communications.manage` | ❌ | ✅ | opt | ❌ | ❌ | ❌ |
| `forms.manage` | ❌ | ✅ | ✅ | ❌ | ❌ | complete own |
| `settings.manage` | ❌ | ✅ | opt | ❌ | ❌ | ❌ |
| `audit.view` | Platform | ✅ | opt | ❌ | ❌ | ❌ |
| `members.invite` (≤ own role) | ❌ | ✅ | ✅ (not owner) | ❌ | ❌ | ❌ |

"opt" means it is off by default and can be granted per membership. The SaaS admin **does not** get implicit tenant data access. Support access goes through audited impersonation (M5.5).

## Risk register

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | The repo doesn't boot (conflict markers, migration fork) | Blocks everything | M0 |
| R2 | Live cross-tenant leaks (T1–T5) | Data breach | M0 hot-fixes the critical three; M1.1–M1.5 fix the pattern |
| R3 | Double booking under concurrency | Core product failure | M4.2: lock + exclusion constraint + PG race tests |
| R4 | PostgreSQL is the reference test DB, but the current dev machine has no Docker or PostgreSQL | The authoritative test run isn't possible locally | Install PostgreSQL 17 locally or Docker Desktop before M0 is closed; CI remains the gate |
| R5 | Existing production data (unknown) has overlaps or odd customer names | Constraint/backfill migrations fail | Pre-check commands, three-step migrations |
| R6 | Legacy app removal loses useful logic | Regressions | No production data (confirmed). Port the lock pattern, transitions and tests **before** deleting (M1.6) |
| R7 | Scope is very large | Stalls | Small milestones, each shippable and tested |
| R8 | Timezone bugs (org vs location vs UTC) | Wrong slots | Location timezone becomes the source of truth; property-based tests around DST |
| R9 | Front-end build adds tooling | Dev friction | Standalone Tailwind binary in Docker; committed built CSS for non-Docker devs |

---

# Milestones

Each milestone is roughly one PR or commit group and leaves `main` green. The format for each is: Objective · Reuse · Models · Backend · UI · Security · Tests · Migration impact · Acceptance.

---

## M0: Baseline repair and emergency isolation fixes (pre-Phase 1)

### Objective
Make the repo boot, migrate and test cleanly. Close the three critical live leaks without waiting for the Phase 1 redesign.

### Existing Components Reused
- Both sides of the stash conflict.
- The existing test suites.
- CI.

### Models
- `accounts.User`: resolve the conflict by keeping **both** sides (`role` plus the email-first fields).
- Add merge migration `accounts/0003_merge`.

### Backend
- Resolve the conflict markers in 4 files.
- Update the legacy tests to the email-first `create_user`/`create_superuser(email=…, password=…, role=…)` signature.
- Fix the `accounts/test_role_permissions.py` expectations.
- `notifications/services.py`: pass `booking_id`/`organization_id` instead of the model, and dispatch with `transaction.on_commit`. The task loads the `Booking` itself.
- Fix `STORAGES` (WhiteNoise) and fail fast on the default `SECRET_KEY` when `DEBUG=False`.
- Delete `command*.txt` and `requiremnts.txt`.
- Make the `Makefile` `PYTHON` overridable (`PYTHON ?= python`).
- Remove `--maxfail=1` from the default `addopts`.
- `entrypoint.sh`: run migrate and collectstatic only for `web` (a role env var or separate command).
- **Hot-fixes:**
  - `WaitlistViewSet` → `IsAuthenticated` + `IsOrganizationManagerOrOwner` (public waitlist joins move to M4.7).
  - `OrganizationDashboardSummaryView` → `IsOrganizationManagerOrOwner`.
  - `CurrentOrganizationView` → PATCH restricted to owner/manager; `is_active`/`is_suspended` read-only.

### UI
None.

### Security
Closes T1, T2 and T3 (the critical findings).

### Tests
- Regression tests for P1, P2 and P3 (they must now return 401/403).
- The full existing suite must be green on SQLite.
- The PG-only tests pass in CI.

### Migration Impact
One merge migration. No schema change.

### Acceptance Criteria
- `manage.py check`, `makemigrations --check`, `pytest` (all), `ruff check`, and `black --check` pass locally (SQLite) and in CI (PostgreSQL).
- The probe tests P1–P3 fail closed.
- Booking creation with no broker running does not block. The notification is sent after commit.

---

## Phase 1: SaaS / tenant foundation

### M1.1: Tenant context resolution

> **Status: done (2026-09-28, branch `m1-tenant-foundation`).** Deviations from the plan below:
> - No separate `Organization.status` field. The existing `is_active`/`is_suspended` flags are
>   kept, and `suspended_at` and `suspension_reason` are added.
> - The web `TenantMiddleware` and `/app/switch/<slug>/` move to M2.5, the first web UI.
>   Resolution already honours the session key, and `set_active_organization()` exists.
> - Details are in `docs/MULTI_TENANCY.md`.

#### Objective
Make tenant resolution safe by construction.

#### Existing Components Reused
- `organizations/selectors.py`.
- `core/middleware.py`.
- `core/permissions.py`.

#### Models
- `Organization`: add `status` (`trial/active/suspended/closed`, kept in sync with `is_suspended`), `suspended_at`, `suspension_reason`.

#### Backend
- New `organizations/tenancy.py`:
  - `resolve_tenant(request) -> TenantContext(organization, membership, capabilities)`. The header/session/query slug is accepted **only** if the user holds an active membership in that org.
  - Deterministic default: the last-used org from the session, else the oldest membership.
  - Suspended orgs raise `TenantSuspended`.
- `TenantMiddleware` sets `request.tenant` for web requests (session). A DRF `TenantAPIView` mixin resolves it lazily for JWT.
- Replace `get_request_organization` call sites; keep a deprecated wrapper that delegates.
- `scope_queryset_by_organization` must no longer give superusers an unfiltered queryset on tenant endpoints.
- `/app/switch/<slug>/` session switch.
- Clear the thread-local request in the `finally` of `RequestAuditMiddleware`.

#### UI
An organization switcher partial (used from M2.5).

#### Security
Closes T6, T7, T8 and T15. The superuser has no implicit tenant access.

#### Tests
- A non-member supplying a header slug → 403 or 404.
- A multi-org user switches correctly.
- A suspended org → 403 with a clear error.
- A superuser on a tenant endpoint without platform context → empty result or 403.

#### Migration Impact
Additive fields plus a backfill of `status` from `is_suspended`/`is_active`.

#### Acceptance Criteria
No view in the codebase calls `Organization.objects.get(slug=…)` from request input except the public booking resolver. A grep check runs in CI.

### M1.2: Roles and capability registry

> **Status: done (2026-09-28, branch `m1-tenant-foundation`).**
> - Views use `HasCapability(read=, write=)`.
> - `docs/PERMISSIONS.md` is generated with `manage.py generate_permissions_doc`, and a test
>   keeps it in sync.
> - Behaviour change: managers no longer edit the organization profile by default
>   (`organization.manage` is owner-only unless granted).

#### Objective
Permission-based authorization.

#### Existing Components Reused
- `OrganizationRole`.
- `user_has_org_role`.
- `IsOrganizationManagerOrOwner` (kept as a thin wrapper).

#### Models
`OrganizationMembership`:
- add the `receptionist` role;
- add `granted_permissions` and `revoked_permissions` JSON lists, validated against the registry;
- add a `title` label.

#### Backend
- `organizations/permissions.py`: a `Capability` enum and `ROLE_CAPABILITIES`.
- `membership.capabilities` (a cached property).
- DRF `HasCapability("customers.manage")` permission factory.
- A web `@require_capability` decorator and `CapabilityRequiredMixin`.
- An object-level helper `can_access_booking(ctx, booking)` covering the staff-own and customer-own rules.
- Invitation role ceiling: the inviter cannot grant a role above their own (fixes T14).

#### UI
None (used by the sidebar in M2.5).

#### Security
- Server-side checks on every view.
- The registry is the single source of truth, and `docs/PERMISSIONS.md` is generated from it.

#### Tests
- A matrix test: for every (role × capability) the expected allow/deny.
- Overrides applied.
- A manager cannot invite an owner.

#### Migration Impact
Additive (choices plus JSON fields). No data change.

#### Acceptance Criteria
The permission matrix in `docs/PERMISSIONS.md` matches the registry, and a test asserts this.

### M1.3: Tenant-safe serializers and viewsets

> **Status: done (2026-09-28, branch `m1.3-tenant-safe-api`).**
> - Beyond the plan below: a basic status-transition table and an atomic reschedule are in
>   `bookings/services.py`, and customers no longer see `internal_notes`.
> - M4.3 still owns the full lifecycle work (per-transition capabilities, check-in/out actions).

#### Objective
Remove the cross-tenant reference and mass-assignment holes.

#### Existing Components Reused
All existing serializers and viewsets.

#### Models
None.

#### Backend
- `core/api.py`:
  - `TenantScopedViewSet` (base queryset `.for_tenant(ctx)`; `perform_create` injects the organization);
  - `TenantPrimaryKeyRelatedField` (its queryset is limited to `request.tenant.organization`), used for all FK and M2M fields.
- Replace `fields="__all__"` with explicit field lists.
- `BookingViewSet` → no generic update or destroy. Only explicit actions (`cancel`, `reschedule`, `transition`) that call services.
- Map domain errors to 400/409 via a DRF exception handler (`core/exceptions.py`: `DomainError`, `ConflictError`, `PolicyViolation`).
- `SlotViewSet`: public-only services and staff, 404 on unknown ids.

#### UI
None.

#### Security
Closes T4, T5, T9, T10 and T13.

#### Tests
- P4, P5 and P6 regressions → 400.
- Unknown or other-tenant ids → 400 or 404 (never 500).
- Customer PATCH on a booking → 405.

#### Migration Impact
None.

#### Acceptance Criteria
- No serializer uses `__all__`.
- Every relational field is tenant-limited. A test introspects all serializers.

### M1.4: Audit framework

> **Status: done (2026-09-28, branch `m1.4-audit-framework`).**
> - The audit helper is `core.audit.record_audit`; the old `write_audit_log` was removed.
> - Plain create/update/delete endpoints are audited through `core.api.AuditedModelViewSetMixin`,
>   which puts the write and its audit row in one transaction.
> - A read-only `/api/v1/audit-logs/` endpoint was added.
> - Deviation: `BookingActivityLog` writes are kept until the CRM activity timeline (M2.3)
>   replaces them, so customer history isn't lost in between.

#### Objective
Consistent, safe audit logging.

#### Existing Components Reused
`AuditLog`, `write_audit_log`.

#### Models
- `AuditLog`: add an index on (organization, created_at) and an `actor_type` field (`user`/`system`/`api_key`/`ai_agent`/`platform_admin`).
- Add an `impersonator` FK (nullable).

#### Backend
- `core/audit.py`:
  - an `AuditAction` constants enum (CUSTOMER_CREATED, …);
  - `audit(ctx, action, obj, changes=…)` that computes field diffs through a **denylist scrubber** (password, token, secret, card, …);
  - proxy-aware client IP (behind `TRUSTED_PROXY_COUNT`).
- Call it from services, not views.
- Retire `BookingActivityLog` writes in favour of the audit log plus the CRM activity (M2.3). Keep the table for history.

#### UI
None yet (the audit screens arrive in M5.1/M5.5).

#### Security
- Metadata is scrubbed.
- The audit log is append-only: no update/delete APIs, and it is read-only in Django admin.

#### Tests
- Scrubber unit tests.
- Every service mutation emits the expected action.

#### Migration Impact
Additive.

#### Acceptance Criteria
Every service-layer mutation in Phases 1–4 writes an audit row, and a test enforces this.

### M1.5: Tenant isolation test suite and factories

> **Status: done (2026-09-28, branch `m1.5-isolation-suite`).**
> - Built: `tests/factories.py` (every model, same-organization defaults), the router-driven
>   `tests/test_tenant_isolation.py`, and tenant-safe relation filters (`core/filters.py`).
>   Detail actions now return 404 before validating input.
> - `seed_demo` was rewritten (Harmony Wellness Centre and Serenity Spa, idempotent, no emails).
> - Fixed: users created outside `create_user` got an empty, colliding `username`.
> - Task-level isolation is covered in `organizations/test_tenant_hotfixes.py`.

#### Objective
Prove that Tenant A cannot see or change Tenant B's data.

#### Existing Components Reused
factory-boy (already a dependency).

#### Models
None.

#### Backend
- `tests/factories.py` with factories for every model.
- `tests/conftest.py` with two-tenant fixtures.
- A parametrized isolation suite that walks **every** router endpoint (list/retrieve/update/delete/search/export actions) as a member of A against B's objects.

#### UI
None.

#### Security
This is the core regression net.

#### Tests
- An auto-discovered endpoint list, so new endpoints are tested automatically, or fail if they aren't registered in the suite.
- Selector-level tests.
- Task-level test: a task given B's id with A's organization context → refused.

#### Migration Impact
None.

#### Acceptance Criteria
- The suite runs in CI.
- Adding an unscoped viewset makes the suite fail.

### M1.6: Legacy removal and single role system

> **Status: done (2026-09-28, branch `m1.6-legacy-removal`). Phase 1 is complete.**
> - Ported the parent-row lock: every create and reschedule locks the provider's
>   `StaffProfile` first (`lock_staff`), closing the empty-slot race. This part of M4.1/M4.2 was
>   pulled forward because the legacy race tests needed a lock to prove.
> - `tests/test_booking_engine.py`: interval rules, the full transition table, lock order, and
>   8 PostgreSQL race tests. They were confirmed to fail without the lock.
> - The working-hours and slot-grid cases from `appointments/test_scheduling.py` were **not**
>   ported, because `create_booking` doesn't validate availability yet. They are the
>   reference list for M4.1's `validate_slot` and can be found in git history before commit
>   `2476181`.
> - The legacy role tests were restated for memberships in
>   `accounts/test_privilege_escalation.py`.
> - Three commits: port; unroute plus `DeleteModel` (apps kept for one commit); remove apps
>   plus `RemoveField User.role`. A database created before M1.6 that skips the middle
>   commit keeps orphan `specialists_*`/`appointments_*` tables, which are safe to drop by
>   hand.

Approved 2026-09-27. There is no production data, so no export step is needed.

#### Objective
One booking domain and one role system.

#### Existing Components Reused
- The legacy tests (their valuable cases are ported).
- `ALLOWED_TRANSITIONS`.
- The `appointments/transactions.py` locking and conflict mapping.
- The `appointments/scheduling.py` interval helpers.

#### Models
- Delete `specialists.Specialist`, `specialists.WorkingHour` and `appointments.Appointment`.
- Remove `User.role` and `UserRole`.

#### Backend
Work in this order:
1. **Port first:** move the transition table, the parent-row lock pattern and the interval helpers into `bookings`/`scheduling`. Port the valuable legacy tests (interval edge cases, PostgreSQL race harness) to target `Booking`/`StaffProfile`.
2. **Unroute:** remove the `specialists`/`appointments` includes from `config/urls.py`.
3. **Delete the models:** add `DeleteModel` migrations inside both apps, so any existing dev databases drop the tables cleanly.
4. **Remove the apps:** in the next commit, remove both apps from `INSTALLED_APPS` and delete their code, `accounts/permissions.py`, and the legacy tests.
5. **Drop `User.role`:** remove the field from `accounts.User` with a `RemoveField` migration, together with `accounts/test_role_permissions.py` or its ported equivalent against membership capabilities.

#### UI
None.

#### Security
Closes T12. Leaves one authorization model: memberships plus capabilities.

#### Tests
- The ported interval, transition and lock tests pass against `Booking`.
- The full suite passes on PostgreSQL.

#### Migration Impact
**Destructive but approved**, and there is no production data:
- `DeleteModel` migrations for both legacy apps (applied before the apps are removed);
- `RemoveField` for `User.role`.

#### Acceptance Criteria
- `/api/specialists/…` and `/api/appointments/…` return 404.
- `grep -r "specialists\|appointments\.\|UserRole"` finds no code references.
- A fresh `migrate` on an empty PostgreSQL database creates no legacy tables.

---

## Phase 2: CRM

### M2.1: Customer model expansion

> **Status: done (2026-09-28, branch `m2.1-customer-model`).** See [CRM.md](CRM.md).
> Deviations from the plan below:
> - The service uses module functions (`create_customer`, `update_customer`,
>   `find_or_create_customer`, `merge_customers`, `anonymize_customer`), matching
>   `bookings.services`, instead of a `CustomerService` class.
> - The never-maintained counters (`total_bookings`, `no_show_count`, `last_appointment`)
>   are dropped. `customer_stats` computes them instead.
> - Added `anonymized_at`. Email uniqueness ignores case (`lower(email)`).
> - The booking engine now creates customers through `find_or_create_customer`, which
>   applies the same checks and audit, never links an account to an existing record, and
>   records the source.
> - `/api/v1/customers/` exposes the new fields, routes writes through the service (audited
>   once), and includes `stats` on retrieve. Merge and anonymize endpoints are M2.4.
> - Booking creation still requires an email. Phone-only booking belongs to the reception
>   flow (M4.5).

#### Objective
A full CRM customer entity.

#### Existing Components Reused
`bookings.Customer`.

#### Models
`Customer` gains:
- `first_name`, `last_name`, `preferred_name`, `secondary_phone`, `birthday`, `gender` (optional/configurable), `pronouns`;
- `address_line1/2`, `city`, `region`, `postal_code`, `country`;
- `preferred_language`, `preferred_contact_method`, `source`, `status` (`active/inactive/archived/anonymized`);
- `assigned_staff` FK and `preferred_staff` FK;
- `marketing_consent`, `email_consent`, `sms_consent` plus `consent_updated_at`;
- `created_by`, `alerts` (short internal alert text).

Also:
- make `email` optional, with a check constraint that email or phone is required;
- replace unique(org,email) with a partial unique constraint (`email` not blank);
- add indexes on (org, last_name, first_name), (org, phone) and (org, status).

#### Backend
- `crm/services.py`: `CustomerService.create/update/merge_duplicate/anonymize`.
- `crm/selectors.py`: `get_customer_for_org`, `list_customers(ctx, filters)`, `customer_stats(customer)` (computed, not denormalized counters).

#### UI
None (M2.6).

#### Security
- Consent changes are audited.
- The anonymization architecture is defined here (it replaces PII and keeps the aggregates).

#### Tests
- Name split backfill.
- The email-or-phone constraint.
- Partial uniqueness.
- Anonymization removes PII.

#### Migration Impact
1. Additive fields.
2. Data migration: split `name` → first/last (last whitespace token = last name). Keep `name` as a legacy field that is kept in sync.
3. Swap the constraints.

#### Acceptance Criteria
Existing customers are preserved, and all bookings still link to them.

### M2.2: Tags

> **Status: done (2026-09-28, branch `m2.2-tags`).** See [CRM.md](CRM.md#tags).
> - `crm.Tag`, `crm.CustomerTag` (with `organization` and `tagged_by`) and `Customer.tag_set`.
>   Migration `crm/0002` backfills tags from the JSON.
> - Services: `create_tag` / `update_tag` / `delete_tag`, `add_customer_tag` /
>   `remove_customer_tag` / `set_customer_tags`. They re-read the customer under a lock, so a
>   stale copy can't tag an anonymized customer.
> - Tagging events are **audited** now. **Activity**-timeline entries arrive with the activity
>   model in M2.3.
> - API: `/api/v1/tags/`, `tags` and `tag_ids` on customers, and the `?tag=` filter. The UI
>   filter is M2.6.
> - `seed_demo` adds VIP, New client, Prefers mornings and Member tags.

#### Objective
Organization-defined tags.

#### Existing Components Reused
The `Customer.tags` JSON data.

#### Models
- `crm.Tag` (org, name, slug, color; unique(org,slug)).
- `Customer.tag_set` M2M through `CustomerTag` (tagged_by, created_at).

#### Backend
- Tag CRUD service.
- Add/remove tag, emitting activity and audit events.
- Filtering and search by tag.

#### UI
M2.6.

#### Security
Tag ids are tenant-validated.

#### Tests
- JSON → Tag backfill.
- A cross-tenant tag assignment is rejected.

#### Migration Impact
- A data migration creates the Tags from the distinct JSON values per org.
- The JSON field is kept read-only until M11.3.

#### Acceptance Criteria
Filtering by tag works through both the API and the UI.

### M2.3: Notes and activity timeline

> **Status: done (2026-09-28, branch `m2.3-notes-activity`).** See [CRM.md](CRM.md#notes).
> - **Models:** `crm.CustomerNote` and `crm.CustomerActivity`. The generic subject is stored
>   as `subject_type` (model label) plus `subject_id`, instead of a ContentType foreign key.
> - **Timeline writers:** `record_activity()` is called from the booking service, the CRM
>   service and the email task. Tag activity from M2.2 is included.
> - **Visibility:** notes default to `internal`. Internal notes and their timeline entries
>   need `customers.notes.private`. Only the author or a capability holder may edit or
>   delete a note.
> - **Backfill:** from **bookings** (not `BookingActivityLog`, which is incomplete) in
>   migration `crm/0004`. `seed_demo` also writes timeline entries and sample notes.
> - **Bug fixed:** reschedule now keeps the booking's CRM customer. Before, if the customer's
>   email had changed, it created a duplicate customer from the old email.
> - **Deferred:** "Staff see their own notes" needs customer access for staff, which is
>   M2.4 (assigned customers).

#### Objective
Internal notes and the customer history.

#### Existing Components Reused
`BookingActivityLog` (history import).

#### Models
- `crm.CustomerNote`: org, customer, author, note_type, **visibility** (`internal`/`customer_visible`), content, pinned, edited_at.
- `crm.CustomerActivity`: org, customer, actor, `kind` enum (customer_created, appointment_booked/rescheduled/cancelled/completed, note_created, email_sent, sms_sent, form_completed, payment_recorded, tag_added, profile_updated), a generic `subject` (content type + id) and `metadata`.

#### Backend
- `crm/activity.py::record_activity()`, called from the Booking, Notification, Forms and CRM services.
- The timeline selector is paginated.

#### UI
M2.6.

#### Security
- Customers only ever see `customer_visible` notes, enforced in the selector.
- Private-note capability.

#### Tests
- Visibility rules (customer API/portal never returns internal notes).
- Activity is emitted by each service.

#### Migration Impact
- New tables.
- An optional backfill from `BookingActivityLog`.

#### Acceptance Criteria
The timeline shows the booking lifecycle events for seeded data.

### M2.4: CRM API and search

> **Status: done (2026-09-28, branch `m2.4-crm-api-search`).** See [CRM.md](CRM.md#search).
> - **Endpoint names:** the history endpoint is `/customers/{id}/timeline/` (from M2.3), not
>   `/activity`. Customer search is `/customers/search/?q=`.
> - **Code moved:** the customer API moved to `crm/views.py` (same URLs).
> - **Provider access:** providers see their assigned or treated customers, read-only, and
>   their own notes. This covers the "own notes" rule deferred from M2.3.
> - **New capability** `customers.erase` for delete and anonymize (owner and manager).
>   Receptionists could previously hard-delete customers.
> - **Phone search:** a digits-only `phone_search` column, plus trigram indexes on
>   `UPPER(name/email)` and `phone_search`, created only on PostgreSQL.
> - **N+1 guard:** a query-count test that shows the list cost doesn't grow with the number
>   of rows (plain `TestCase`, not the pytest fixture).
> - **Benchmark** (`benchmark_customer_search`, 50k customers, local PostgreSQL 18): worst
>   p95 86.5 ms, from name words that match thousands of rows. Phone and email take about
>   3 ms. The target is under 100 ms.

#### Objective
Customer endpoints and organization-scoped global search.

#### Existing Components Reused
`CustomerViewSet`, DRF `SearchFilter`.

#### Models
- Indexes: trigram on the name/email/phone combination (PostgreSQL, `pg_trgm` guarded), with a fallback to `icontains`.

#### Backend
Endpoints:
- `/api/v1/customers/` (filters: tag, status, staff, last visit);
- `/api/v1/customers/{id}/notes|activity|appointments`;
- `/api/v1/customers/search?q=` (phone normalization);
- `/api/v1/search?q=` (customers, appointments by reference, staff, services), with capped results per type.

#### UI
M2.6 (search box).

#### Security
- Search results are filtered by capability. Staff see only assigned or own customers when they lack `customers.view`.

#### Tests
- Search isolation (M1.5 extended).
- Phone formats.
- N+1 guard (`django_assert_max_num_queries`).

#### Migration Impact
Index-only (conditional on PostgreSQL).

#### Acceptance Criteria
Search p95 under 100 ms on 50k seeded customers (PostgreSQL).

### M2.5: Web app shell, session auth, component library

> **Status: done (2026-09-28, branch `m2.5-web-shell`).**
> - **Pages:** `/login/`, `/logout/` (POST), `/password-reset/`, `/reset-password/` and
>   `/verify-email/` (the links our emails already sent), `/accept-invitation/` (join, or
>   create an account and join), `/home/` (role-based redirect), `/app/dashboard/`,
>   `/staff/dashboard/`, `/portal/`, `/saas/`, `/app/switch/<slug>/`.
> - **Beyond the plan, to exercise the shell:** a read-only Team page (`members.view`) and
>   Audit log page (`audit.view`), both with search or filters and htmx pagination; a
>   development-only component gallery at `/app/components/`.
> - **Permissions:** pages use `TenantPageMixin.required_capabilities`; the sidebar
>   (`core/navigation.py`) filters on the same capabilities, and a test checks that every
>   link is shown exactly when its page returns 200 for each role.
> - **Password reset** keeps our Celery email with `SITE_URL` links instead of Django's
>   `PasswordResetView`, which builds links from the Host header.
> - **Dashboard numbers:** "today", "this week" and "this month" are now calendar
>   periods in the organization's time zone and leave out cancelled and rejected
>   appointments. They were UTC dates, and the week and month counts included every later
>   appointment too. Pages render dates in the organization's time zone.
> - **Assets:** htmx 2.0.11, Alpine 3.17.4 (CSP build) with the focus plugin, Tailwind
>   4.3.3 standalone CLI (`manage.py tailwind build`). Chart.js was dropped from the base
>   template (unused); it will be vendored with the reports.
> - **Renamed:** the API's URL names now start with `api-` (for example `api-login`), so
>   the web pages can use `login`. Paths are unchanged. The product name is the `SITE_NAME`
>   setting (default `BookCRM`).
> - **Accessibility:** Lighthouse scored 100 for accessibility on the sign-in, dashboard
>   and Team pages, on both mobile and desktop (headless Chrome, 2026-09-28).

#### Objective
The foundation for every UI area.

#### Existing Components Reused
`base.html`, accounts services.

#### Models
None.

#### Backend
- Session login by email (Django `LoginView` with an email form), logout, password reset (Django views), and invitation acceptance pages.
- Role-based home redirect: platform → `/saas/`; member → `/app/dashboard/` (or `/staff/dashboard/` for staff-only); customer → `/portal/`.
- A context processor for tenant, capabilities and nav.
- Login throttling.

#### UI
- `templates/layouts/{public,app,saas,portal}.html`.
- `templates/components/` partials: button, card, table, form field, dropdown, modal (Alpine), alert/toast, badge, empty state, pagination, search, filter bar, tabs.
- A sidebar driven by capabilities.
- Tailwind standalone build (`static/src/app.css` → `static/dist/app.css`).
- Vendored htmx and alpine.

#### Security
- CSRF header for HTMX.
- A CSP header (self-hosted assets now make one possible).
- Login and password-reset throttles.

#### Tests
- Login and logout.
- Each role lands on the right home.
- Nav items hidden **and** URLs return 403 without the capability.

#### Migration Impact
None.

#### Acceptance Criteria
- Pages render on mobile and desktop.
- No CDN scripts remain.
- Lighthouse accessibility score of 90 or more on the shell.

### M2.6: CRM UI

> **Status: done (2026-09-29, branch `m2.6-crm-ui`).**
> - **Pages:** `/app/customers/` (search, status and tag filters, htmx paging, "New
>   customer" dialog) and `/app/customers/<uuid>/<tab>/` with the tabs Overview,
>   Appointments, Notes, Communications, Forms, Transactions and Activity. Each tab is its own
>   URL; htmx swaps only the tab area.
> - **Overview:** first, last and next visit, totals, cancellations, no-shows, alerts, tags,
>   assigned and preferred providers, consent. "Spend to date" (completed appointments) is
>   shown to `customers.view` holders, with a note that predicted lifetime value comes later.
>   `customer_stats` gained `first_visit`.
> - **Without a page reload:** create (dialog), edit (dialog), tag (tick boxes, or type a new
>   tag, which is created or reused), and notes (add, edit in a dialog, pin, delete). Every
>   form also works as a normal page without JavaScript. Form errors come back as 422 and are
>   shown in place (htmx `responseHandling`).
> - **Communications** lists the email and SMS timeline entries. **Forms** and
>   **Transactions** are labelled placeholders (M6 forms; payments later).
> - **Header search:** a search box in the app header (dropdown of matches, or a results
>   page). The API's `/api/v1/search/` and the header share `core/search.py`.
> - **One set of rules:** the checks that were inside the API permission classes moved to
>   plain functions in `crm/permissions.py` (`can_browse_customers`, `can_write_notes`,
>   `can_change_note`, ...), used by the API and the pages alike. The sidebar item uses the
>   same function as the page (`NavItem.allow`).
> - **Tag colours** are free hex values but the CSP forbids inline styles, so each tag shows
>   the nearest colour of a fixed palette (`crm/templatetags/crm_tags.py`).
> - **Fixed along the way:** a multi-line `{# #}` comment in `base.html` was printed into every
>   page (now `{% comment %}`, with a test); the page dialog now closes after a swap
>   (`HX-Trigger-After-Swap`); the phone header no longer overflows by 2px.
> - **Review 3 (Codex, 2026-09-29), fixed in the same PR:** a note's timeline entry now
>   follows visibility changes (F1); authors change their own internal notes only while still
>   allowed to write them (F2); password reset is one locked, re-checked step (F3); adding the
>   same new tag concurrently no longer errors (F4); deleting a note without JavaScript asks
>   first (F5); the acceptance test now says it checks fragments, not the browser (F6). Tests:
>   `tests/test_review_3_findings.py`.
> - **Checked:** headless Chrome walkthrough of the receptionist flow (no CSP violations or
>   script errors); Lighthouse accessibility and best practices 100 on the list, profile,
>   notes and activity pages, mobile and desktop.

#### Objective
The customer list and the 360° profile.

#### Existing Components Reused
M2.1–M2.5.

#### Models
None.

#### Backend
Web views calling the CRM services and selectors.

#### UI
- `/app/customers/`: a table with search, tag and status filters, HTMX pagination, and a create modal.
- `/app/customers/<uuid>/`: tabs Overview | Appointments | Notes | Communications | Forms | Transactions (placeholder) | Activity.
  - Overview shows: first, last and next visit; totals; cancelled; no-shows; alerts; tags; assigned and preferred staff; a clearly labelled CLV placeholder.

#### Security
- Capability checks per tab.
- The staff own-scope applies.

#### Tests
- View permission tests.
- An HTMX partial smoke test.
- Isolation (a B customer UUID from A → 404).

#### Migration Impact
None.

#### Acceptance Criteria
A receptionist can find, create, tag and annotate a customer without a full page reload.

---

## Phase 3: Locations, staff and services

### M3.1: Locations

> **Status: done (2026-09-29, branch `m3.1-locations`).** See [LOCATIONS.md](LOCATIONS.md).
> - **Models:** `Location` (with `is_default`, one per organization, always active),
>   `LocationHours` (up to two periods a day) and `LocationClosure` (a date range, all day or
>   between two times each day). Each row stores `organization` for scoping without joins.
> - **Default location:** created as "Main" for every existing organization by migration
>   `locations/0002`, and for new organizations by a `post_save` signal, so no code path can
>   create an organization without one.
> - **Plan limit:** the plan gained `maximum_locations` (default 1; the demo plans allow 1, 3
>   and 20). It counts active locations and is checked through `enforce_plan_limit`, with the
>   organization row locked. A full `FEATURE_MULTI_LOCATION` entitlement model is M8.1.
> - **New capability `locations.view`** for every team role; `locations.manage` stays with
>   owners and managers.
> - **API:** `/api/v1/locations/` (plus `hours` and `make-default` actions) and
>   `/api/v1/location-closures/`, both covered by the tenant isolation suite.
> - **UI:** `/app/locations/` list and a location page with an hours editor and closures, in the
>   sidebar under Organization.
> - **Deviation:** web URL names are prefixed `app-location-…`, because the API router already
>   uses `location-list` and `location-detail`.

#### Objective
Multi-location organizations.

#### Existing Components Reused
Organization address and timezone.

#### Models
- `locations.Location`: org, name, slug, address fields, `timezone`, phone, email, `is_active`, `booking_enabled`, `booking_settings` JSON.
- `locations.LocationHours`: location, weekday, open, close.
- `locations.LocationClosure`: date, partial or full (this generalizes `OrganizationHoliday`, which stays as an org-wide closure).

#### Backend
- `LocationService` CRUD.
- Selectors.
- API at `/api/v1/locations/`.

#### UI
`/app/locations/` list and form, including an hours editor.

#### Security
- `locations.manage`.
- Multi-location count gated by the entitlement (M8.1 hooks; `FEATURE_MULTI_LOCATION`).

#### Tests
- CRUD.
- Isolation.
- The default location exists for every org.

#### Migration Impact
- New tables.
- Data migration: create a default "Main" location per org from `Organization.address` and `timezone`.

#### Acceptance Criteria
Every org has at least one location.

### M3.2: Staff and service assignments

> **Status: done (2026-09-29, branch `m3.2-staff-assignments`).** See [STAFF.md](STAFF.md).
> - **Models:** `StaffProfile` gained `display_name`, `provider_type`,
>   `online_booking_visible`, `max_daily_appointments` and `locations`;
>   `StaffServiceOffering` (location empty = all their locations; custom duration and price).
> - **Backfill:** migration `staff/0003` puts every staff member at their organization's
>   default location and turns each `assigned_staff_members` link into an "all locations"
>   offering. The M2M stays as a mirror written only by `staff/services.py`; the services API
>   still accepts it and converts it to offerings.
> - **Services and selectors:** `create_staff_profile` (active non-customer member, staff limit
>   through `enforce_plan_limit`), `set_staff_locations`, offering CRUD, `set_staff_offerings`,
>   `list_providers_for(service, location, public=)` and `offering_for`.
> - **API:** `/api/v1/staff/` writes through the services; new `/api/v1/staff-offerings/` (in
>   the isolation suite) and `GET /services/{id}/providers/`.
> - **Public exposure:** the booking page shows `public_name` and lists, like the slot API and
>   self-service bookings, only staff visible online. (Email addresses were already hidden
>   since the 2026-09-28 review.)
> - **UI:** `/app/staff/` and a person page with tabs Profile, Services, Locations,
>   Availability and Time off (the last two read-only until M3.4).
> - **Deferred:** the booking engine enforcing offerings and the daily limit (M3.4/M4.1);
>   inviting new team members from the web app (only via the API today).

#### Objective
Staff ↔ locations ↔ services.

#### Existing Components Reused
`Service.assigned_staff_members` (kept as the "all locations" assignment).

#### Models
- `StaffProfile.locations` M2M.
- `StaffServiceOffering` (staff, service, location nullable = all locations, optional custom duration and price).
- `StaffProfile`: `max_daily_appointments`, `online_booking_visible`, `display_name`, `provider_type`.

#### Backend
- An `AssignmentService`.
- Selectors `list_providers_for(service, location)`.
- Staff create/invite flow: `staff.manage`, enforcing the staff limit via `enforce_plan_limit` (M8.1).

#### UI
`/app/staff/` list and detail with tabs Profile | Services | Locations | Availability | Time off.

#### Security
- The staff's user must be a member of the org (validated).
- Staff profiles are not exposed publicly beyond `display_name`, bio and photo (fixes the staff-email leak on the public page).

#### Tests
- The assignment matrix.
- Public provider listing shows no email addresses.

#### Migration Impact
- Backfill `StaffProfile.locations` with the default location.
- Backfill `StaffServiceOffering` from `assigned_staff_members`.

#### Acceptance Criteria
A provider can offer service X only at location Y.

### M3.3: Service enhancements

> **Status: done (2026-09-29, branch `m3.3-services`).** See [SERVICES.md](SERVICES.md).
> - **Models:** `Service` gained `locations` (M2M), `required_provider_type`, `tax_rate` and
>   `cancellation_policy`; `ServiceCategory` gained `color` and `sort_order`.
> - **Deviations:** no `online_bookable` column: the existing `is_public` already means that
>   and the UI calls it "bookable online". No location backfill: an empty `locations` means
>   "every location", which also covers locations added later (the plan's backfill to "all
>   current locations" would have excluded them). `min_notice_minutes` keeps its name.
>   The editor is the page dialog, not a separate drawer component.
> - **Services layer:** `services/services.py` (unique slugs, plan service limit on create and
>   unarchive, value checks, `in_use` on deleting a booked service, category CRUD with
>   case-insensitive unique names) used by the API, the web app and `seed_demo`.
> - **Rules shared with staff:** an offering must match the service's required provider type and
>   the service's locations; `list_providers_for` applies both. `services_bookable_at(org,
>   location, public=)` is the acceptance selector for the public wizard.
> - **UI:** `/app/services/` grouped by category (colour, position), location and status
>   filters, dialogs for services and categories.
> - **Plan change:** the public booking wizard (M4.6) now follows M4.1 + M4.2.
>
> **Review 4 (Codex, 2026-09-29) on M3.1–M3.3, all fixed in this branch** (tests in
> `tests/test_review_4_findings.py`): F1 compound API writes (a service and its providers, a
> staff profile and its locations) are now one transaction (`AuditedModelViewSetMixin` wraps
> service-audited writes too); F2 staff reactivation locks the organization before the plan
> count (PostgreSQL race test); F3 the data migrations `locations/0002` and `staff/0003`
> reverse as no-ops instead of deleting rows created later; F4 the public page, public slot
> API and self-service booking API refuse a provider who doesn't offer the service; F5 the
> `assigned_staff_members` mirror lists only providers whose offering still fits the service's
> provider type and locations, and is rebuilt when those rules or a provider type change; F6
> category writes lock the organization and turn a unique-constraint race into 409
> `duplicate`; F7 closure reason changes are audited as "changed" without the text.

#### Objective
Complete service configuration.

#### Existing Components Reused
`Service`, `ServiceCategory`.

#### Models
- `Service`: `locations` M2M, `online_bookable` (backfilled from `is_public`), `required_provider_type`, `tax_rate` (Decimal; placeholder for a tax model later), `booking_lead_time_minutes` (≈ `min_notice_minutes`, kept as that name), a `cancellation_policy` text.
- `ServiceCategory`: `color`, `sort_order`.

#### Backend
Service CRUD service. Service count checked against the entitlement.

#### UI
`/app/services/`: grouped by category, with an edit drawer.

#### Security
`services.manage`.

#### Tests
- Location/service availability filtering.

#### Migration Impact
Additive plus backfills (locations = all org locations).

#### Acceptance Criteria
The public wizard lists only services that are online-bookable at the chosen location.

### M3.4: AvailabilityService rewrite

> **Status: done (2026-09-29, branch `m3.4-availability`).** See
> [BOOKING_ENGINE.md](BOOKING_ENGINE.md#availability-schedulingavailabilitypy).
> - `scheduling/availability.py`: `AvailabilityService` with `get_available_slots` (one
>   provider or any, candidates per slot) and `validate_slot` sharing one calculation; batch
>   loading within `QUERY_BUDGET` = 10 queries (tested: 10 providers × 30 days).
> - `WeeklyAvailability.location` (nullable = any of their locations), backfilled to the
>   default location by `scheduling/0003`. Several `AvailabilityException` rows per date were
>   already allowed; they now combine (off all day wins; otherwise their times are the hours).
> - Covers location opening hours, closures, holidays, time off, appointments with buffers
>   (the larger buffer on each side), the daily appointment limit, provider durations,
>   notice/advance limits for the public, and DST (tested both transitions).
> - The public slot API uses it (any provider, location, date ranges), and the public booking
>   page and self-service booking API validate the chosen time with it (409 when taken).
>   The old `scheduling/services.py::generate_slots` (queries per slot) is removed.
> - **Deferred to M4.1:** team bookings through `create_booking` calling `validate_slot`, and
>   bookings using the provider's custom duration and price. There were no legacy interval
>   tests left to port (the legacy apps were removed in M1.6); the interval helpers have
>   their own tests.

#### Objective
A correct, fast, location-aware slot engine.

#### Existing Components Reused
- `scheduling/services.py::generate_slots` (the algorithm outline).
- The legacy interval helpers.

#### Models
- `WeeklyAvailability.location` FK (nullable → backfilled to default).
- `AvailabilityException`: allow several rows per day.

#### Backend
`scheduling/availability.py::AvailabilityService.get_available_slots(org, service, location, date_range, staff=None|ANY)`:
- **batch-loads** availability, exceptions, time off, closures and bookings once per request, with no per-slot queries;
- intersects staff availability with location hours;
- subtracts closures, exceptions, time off, and bookings expanded by buffers;
- applies notice and advance limits;
- computes everything in the location timezone, with DST handled;
- for "any provider", returns slots with the candidate providers.

`validate_slot(...)` reuses the same code, and `BookingService` calls it.

#### UI
None (used by M4.x).

#### Security
Public callers only see online-bookable services and visible staff.

#### Tests
- Ported legacy interval tests.
- DST transitions.
- Buffers.
- Partial closures.
- Any-provider.
- Query-count ceiling.

#### Migration Impact
Additive plus backfill.

#### Acceptance Criteria
A 30-day range for 10 staff is computed in ≤ 10 queries.

---

## Phase 4: Booking enhancements

### M4.1: BookingService consolidation

#### Objective
One validated booking engine for every entry point.

#### Existing Components Reused
- `create_booking` and `cancel_booking`.
- Legacy lock pattern.

#### Models
`Booking` gains:
- `location` FK (nullable → backfilled);
- `booking_source` (`customer_portal`, `public_booking`, `reception`, `staff`, `api`, `ai_agent`, `import`, `admin`);
- `created_by` FK;
- `buffer_before/after_snapshot`;
- `idempotency_key` (unique per org, nullable).

Also make the `reference` year dynamic.

#### Backend
`bookings/booking_service.py::BookingService` with `create`, `reschedule` (atomic; moves the booking in place and records history, or keeps the `rescheduled_from` chain while cancelling the old one **inside the same transaction**), `cancel` (deadline policy with a staff override capability) and `transition`:
- locks `StaffProfile` with `select_for_update`;
- re-runs `AvailabilityService.validate_slot`;
- checks the entitlements' monthly limit;
- upserts the customer via `CustomerService`;
- records history, audit and CRM activity;
- runs notifications `on_commit`.

Domain errors map to 400/409/422. The API, web, `seed_demo` and admin actions all go through it.

#### UI
None (M4.4–M4.6).

#### Security
- A customer can only act on their own bookings, within policy.
- Staff act on their own bookings.
- Reception and managers act on all.

#### Tests
- P7 regression (outside availability → 409/422).
- A failing reschedule leaves the original intact.
- Idempotency-key replay returns the same booking.
- Every source is recorded.

#### Migration Impact
Additive plus a location backfill.

#### Acceptance Criteria
No code outside `BookingService` writes `Booking.status`, `start_datetime` or `end_datetime`. A grep test enforces this.

### M4.2: Database-level conflict protection and concurrency tests

#### Objective
Guarantee that no double booking can occur.

#### Existing Components Reused
The `appointments/test_transactions.py` PG race harness.

#### Models
- `Booking`: add `blocked_start`/`blocked_end` (start/end including buffers, maintained by the service).
- PostgreSQL `ExclusionConstraint` on (staff =, tstzrange(blocked_start, blocked_end, '[)') &&), with a condition on the active statuses.
- `CREATE EXTENSION btree_gist` (guarded to PostgreSQL).

#### Backend
- `manage.py check_booking_overlaps` (a pre-check that lists offenders).
- The exception handler maps `IntegrityError` with the constraint name to 409 "Slot no longer available".

#### UI
None.

#### Security
Not applicable.

#### Tests
`@pytest.mark.postgres`:
- two threads or connections booking the same staff and time → exactly one 201 and one 409;
- overlapping (not identical) intervals;
- a reschedule racing a create;
- a cancellation releasing the slot.

Unit test on SQLite: the lock path runs.

#### Migration Impact
- A backfill of the blocked range.
- The pre-check must report zero overlaps before the constraint migration. If it doesn't, the migration aborts with the list of offenders.

#### Acceptance Criteria
The race tests are green in CI across 50 repeated runs.

### M4.3: Status workflow and history

#### Objective
A strict lifecycle.

#### Existing Components Reused
`BookingStatusHistory`, `ALLOWED_TRANSITIONS`.

#### Models
`BookingStatusHistory`: add `reason` and `source`. Keep `note` for compatibility.

#### Backend
A transition table:
- pending → confirmed / cancelled / rejected;
- confirmed → checked_in / cancelled / no_show;
- checked_in → in_progress / completed / no_show;
- in_progress → completed.

A capability is required per transition (for example, customers can only cancel). Check-in and check-out actions. No-show and completed update the CRM activity.

#### UI
Status badges and action menus (M4.4/M4.5).

#### Security
Transition authorization.

#### Tests
- A full transition matrix.
- History written for every change.

#### Migration Impact
Additive.

#### Acceptance Criteria
An illegal transition → 409. Every status change has a history row.

### M4.4: Calendar

#### Objective
Day, week and month calendars.

#### Existing Components Reused
Booking selectors.

#### Models
None.

#### Backend
- `/app/calendar/events.json`: range-bounded, filterable by location, staff, service and status, minimal fields, `select_related`.
- A resource view (staff columns) for day view.

#### UI
- FullCalendar, vendored and wired to the JSON feed.
- An HTMX side panel for appointment detail and actions.
- Colour by service or provider.
- Drag-and-drop deferred (it would call `BookingService.reschedule`).

#### Security
- The feed is capability and own-scope filtered.
- The feed never includes internal notes for roles without `customers.notes.private`.

#### Tests
- Feed isolation.
- Filter correctness.
- Query count.

#### Migration Impact
None.

#### Acceptance Criteria
A week view with 500 events loads in under 300 ms on the server.

### M4.5: Reception and staff booking flows

#### Objective
Operational booking UI.

#### Existing Components Reused
BookingService, AvailabilityService, CRM search.

#### Models
None.

#### Backend
Web views for:
- new booking (a customer typeahead, or create a customer inline);
- reschedule;
- cancel (reason);
- walk-in (starts now, `checked_in`);
- check-in and check-out.

#### UI
- `/app/appointments/` list with filters.
- A new-appointment modal wizard (HTMX).
- Appointment detail with history.

#### Security
`appointments.manage`, and the staff own-scope.

#### Tests
- The end-to-end flow via the Django client.
- The source is recorded as `reception` or `staff`.

#### Migration Impact
None.

#### Acceptance Criteria
A receptionist books, reschedules and cancels without leaving the page.

### M4.6: Public booking wizard

> **Order:** built right after M4.1 + M4.2, before M4.3–M4.5 (see the PR sequence below).

#### Objective
A polished public self-booking flow.

#### Existing Components Reused
`PublicBookingPageView` (replaced).

#### Models
`Organization`: `booking_instructions`, `primary_color` (branding), `require_account` flag.

#### Backend
- `/book/<slug>/` HTMX steps: location → service → provider (or any) → date → time → details → review → confirmation.
- Server-side wizard state in the session, keyed by org.
- `BookingService.create(source=public_booking)`.
- Optional account creation after booking (a verification email).

#### UI
- A branded, mobile-first wizard.
- An accessible date and time picker.
- Confirmation at `/book/<slug>/confirmation/<public_uuid>/` showing no PII beyond what was entered.

#### Security
- Rate limiting per IP and org.
- A honeypot field.
- Only public services and visible staff (display name only).
- Every step revalidates on the server.
- The confirmation page uses the unguessable `public_uuid` (not the reference).

#### Tests
- A full wizard run.
- A tampered service or staff id from another org → 404.
- A slot taken between steps → friendly 409 message.
- Throttling.

#### Migration Impact
Additive branding fields.

#### Acceptance Criteria
The wizard works without JavaScript on the essential steps (progressive enhancement) and scores 90+ on mobile Lighthouse.

### M4.7: Waitlist enhancement

#### Objective
A useful waitlist.

#### Existing Components Reused
`WaitlistEntry`.

#### Models
`WaitlistEntry`: add `customer` FK, `location`, `time_of_day` preference (`morning`/`afternoon`/`evening`/`any`), `notified_at`, `expires_at`.

#### Backend
- `WaitlistService.join` (public or portal, throttled), `match_for_slot(booking_cancelled)`.
- On cancellation, `on_commit` queues a matching task (notification via M7).
- `/app/waitlist/` management.

#### UI
- Reception waitlist view.
- Portal "join waitlist".

#### Security
Public join creates the entry only. It never reveals other entries (T1 is permanently fixed).

#### Tests
- Matching rules.
- Isolation.
- Anonymous listing is impossible.

#### Migration Impact
Additive plus a backfill of `customer` by email.

#### Acceptance Criteria
A cancellation produces notifications to matching waitlisted customers (once each).

---

## Phase 5: Role-based UI

### M5.1: Owner and manager dashboard

#### Objective
The organization dashboard.

#### Existing Components Reused
`dashboard/selectors.py` (moved into `ReportingService`).

#### Models
None.

#### Backend
Aggregations using `Count`/`FILTER`, date-bounded, with location and provider filters. Cached for 60 s per tenant and filter.

#### UI
Widgets:
- today and this week;
- new customers;
- cancellation and no-show rate (with an "insufficient data" state when n < 20);
- revenue placeholder labelled "estimated from completed appointments";
- upcoming appointments;
- popular services;
- top providers;
- a volume chart (Chart.js, vendored).

#### Security
- `reports.view` for the analytics widgets.
- Operational widgets only for others.

#### Tests
- Numbers match fixtures.
- Isolation.
- Empty states.

#### Migration Impact
None.

#### Acceptance Criteria
The dashboard renders in ≤ 12 queries.

### M5.2: Reception dashboard

#### Objective
An operational front-desk view.

#### Existing Components Reused
Calendar feed, waitlist, BookingService.

#### Models
None.

#### Backend
Today's timeline, staff availability now, checked-in (waiting), next available slot per service, today's cancellations, waitlist count.

#### UI
- `/app/reception/`, auto-refreshing through HTMX polling.
- Quick actions: new booking, new customer, reschedule, search, walk-in.

#### Security
Receptionist capabilities only. No settings or billing links.

#### Tests
- A receptionist cannot open `/app/settings/` or `/app/subscription/` (403).

#### Migration Impact
None.

#### Acceptance Criteria
A walk-in can be checked in within 3 interactions.

### M5.3: Provider area

#### Objective
The staff experience.

#### Existing Components Reused
Calendar and CRM selectors with own-scope.

#### Models
None.

#### Backend
`/staff/dashboard/` ("Good morning, {name}"): today, next, this week, cancellations, customers seen.

#### UI
- `/staff/calendar/`, `/staff/customers/` (assigned or seen only), `/staff/availability/` (own weekly hours and time-off requests).
- Block-time quick action.

#### Security
- Strict own-scope.
- Access to another provider's appointment → 404.

#### Tests
Own-scope isolation tests.

#### Migration Impact
`TimeOff.approval_status` workflow values (additive).

#### Acceptance Criteria
A provider sees only their own schedule and customers.

### M5.4: Customer portal

#### Objective
Customer self-service.

#### Existing Components Reused
Public wizard steps, BookingService.

#### Models
None. Customer ↔ User links already exist, one per organization.

#### Backend
`/portal/` lists the organizations where the user has a Customer record, and each organization's data is scoped separately.
- Pages: dashboard (next appointment, upcoming, quick book, recent visits), book, appointments (upcoming and history, cancel and reschedule within policy), profile and communication preferences (consents), forms (M6.2).

#### UI
A portal layout.

#### Security
- Customers never see another organization's data, even when the same email is used in both.
- Customers never see internal notes.

#### Tests
- A cross-org same-email customer isolation test.
- Policy enforcement.

#### Migration Impact
None.

#### Acceptance Criteria
A customer books, reschedules and cancels within policy from the portal.

### M5.5: SaaS administration

#### Objective
A separate platform admin UI.

#### Existing Components Reused
`IsPlatformAdmin`, Plan, Subscription, AuditLog.

#### Models
- `User.is_platform_staff` (separate from `is_superuser`).
- `saas.Announcement`.
- `saas.FeatureFlag` (global or per-org override).
- `saas.ImpersonationSession` (admin, target user, org, reason, started, ended).

#### Backend
Pages:
- `/saas/` dashboard: tenants total, active, trial and paying; MRR computed from active subscriptions × plan price; new registrations; subscription distribution; appointment volume; active users; system alerts (failed notifications, Celery heartbeat).
- Organizations: list, detail, create, suspend and reactivate (audited, with a reason).
- Subscriptions, plans, users, feature flags, audit log, settings.

Impersonation:
- time-boxed;
- banner shown;
- every action audited with the impersonator;
- impersonating other platform staff is not allowed.

#### UI
The SaaS layout.

#### Security
- Platform guard on all `/saas/` routes.
- Step-up re-authentication before impersonation.
- Tenant data is reachable only through impersonation.

#### Tests
- Non-platform users get 404 or 403.
- Suspending an org blocks its users (M1.1).
- Impersonation is audited and expires.

#### Migration Impact
New tables plus a field.

#### Acceptance Criteria
All SaaS operations are possible without using `/admin/`.

---

## Phase 6: Forms

### M6.1: Form templates and builder

#### Objective
Generic intake, consent and questionnaire forms.

#### Existing Components Reused
None.

#### Models
`forms` app:
- `FormTemplate` (org, name, kind, is_active, version);
- `FormQuestion` (template, order, type: text / textarea / number / date / yes_no / select / multi_select / signature_placeholder, label, help, required, options JSON).

Also a service link (auto-assign on booking).

#### Backend
- `FormService` CRUD with versioning. Editing a published template creates a new version.
- API at `/api/v1/forms/`.

#### UI
`/app/forms/` builder: an HTMX question list with reorder.

#### Security
- `forms.manage`.
- Question options are validated.

#### Tests
- Versioning.
- Isolation.

#### Migration Impact
New tables.

#### Acceptance Criteria
An owner builds a 10-question intake form.

### M6.2: Assignments, submissions, portal completion

#### Objective
Customers complete their assigned forms.

#### Existing Components Reused
Portal, CRM activity.

#### Models
- `FormAssignment` (org, template version, customer, booking?, due, status, token).
- `FormSubmission` (assignment, submitted_at, ip).
- `FormAnswer` (submission, question, value JSON).

#### Backend
- Auto-assign on booking for linked services.
- Manual assignment from the CRM.
- Email link (a signed token) for guests.
- Completion emits a `form_completed` activity.

#### UI
- Portal "Forms".
- The CRM Forms tab shows submissions (read-only).

#### Security
- Answers visible only with `customers.view`.
- Token links are single-customer and expire.
- Answers are never included in audit metadata.

#### Tests
- Type validation per question type.
- Token expiry.
- Isolation.

#### Migration Impact
New tables.

#### Acceptance Criteria
A booking for a linked service triggers form assignment and completion end to end.

---

## Phase 7: Notifications

### M7.1: Notification pipeline and channel abstraction

#### Objective
Reliable asynchronous delivery.

#### Existing Components Reused
`NotificationLog`, `send_templated_email`.

#### Models
`NotificationLog`:
- channels `email`/`sms`/`push`/`whatsapp`;
- `dedupe_key` unique;
- `scheduled_for`;
- `provider_message_id`;
- `payload` (ids only).

#### Backend
- `notifications/channels.py`: `NotificationChannel` protocol; `EmailChannel` (Django mail); `SmsChannel` (a `ConsoleSmsProvider` stub, with a Twilio adapter later).
- `NotificationService.notify(event, booking_id, org_id)` resolves the recipients, the consents and the template, then dispatches `on_commit`.
- The task re-checks tenant and consent at send time.
- Invitation, verification and reset emails move onto the same pipeline.

#### UI
The delivery log on the CRM Communications tab.

#### Security
- Consent respected (`email_consent`, `sms_consent`).
- No PII in task args beyond ids.

#### Tests
- Dispatch only after commit (a rollback → nothing is sent).
- Retry.
- Dedupe.
- Consent opt-out.

#### Migration Impact
Additive.

#### Acceptance Criteria
- The P9 and P10 regressions are fixed.
- The worker processes the email with Redis present.
- Booking does not block without it.

### M7.2: Organization notification templates

#### Objective
Configurable content per organization.

#### Existing Components Reused
`templates/emails/*`.

#### Models
`NotificationTemplate`: org nullable = the platform default; event; channel; subject; body_text; body_html; is_active.

#### Backend
- Rendering in a Django sandboxed template context with an allow-listed variables catalogue.
- Preview endpoint.

#### UI
`/app/communications/templates/` editor with preview.

#### Security
- `communications.manage`.
- Template syntax is restricted (no arbitrary tags or filters).
- Output is escaped.

#### Tests
- Fallback to the default.
- Disallowed tags are rejected.

#### Migration Impact
New table, seeded with the defaults.

#### Acceptance Criteria
An org customizes its confirmation email and the change is used.

### M7.3: Reminder engine

#### Objective
Configurable reminders.

#### Existing Components Reused
The `Organization.reminder_*` fields (migrated).

#### Models
`ReminderRule`: org, offset minutes (1440, 2880, 120, custom), channel, is_active. The rules are backfilled from the org fields.

#### Backend
- A Celery beat task every 5 minutes selects bookings due for each rule within a window.
- It creates a `NotificationLog` with `dedupe_key = reminder:{booking}:{rule}:{start_ts}`.
- Reschedules reset the key through the start timestamp.
- Cancelled bookings are skipped.

#### UI
Settings → Reminders.

#### Security
The tenant is carried explicitly in the task. The query is per org.

#### Tests
- No duplicates across repeated beats.
- The reschedule case.
- Timezone correctness.
- The beat schedule is registered.

#### Migration Impact
New table plus backfill.

#### Acceptance Criteria
A 24-hour reminder is sent exactly once for each booking.

---

## Phase 8: Subscriptions and billing

### M8.1: Entitlements and limits

#### Objective
Stop hard-coding plan logic.

#### Existing Components Reused
Plan, Subscription, `enforce_plan_limit`.

#### Models
- `Feature` (code: `FEATURE_MULTI_LOCATION`, `FEATURE_SMS`, `FEATURE_ADVANCED_REPORTS`, `FEATURE_API_ACCESS`, `FEATURE_AI_RECEPTIONIST`, `FEATURE_CUSTOM_BRANDING`; kind boolean or limit).
- `PlanFeature` (plan, feature, enabled, limit_value).
- `OrganizationFeatureOverride`.

#### Backend
- `subscriptions/entitlements.py`: `has_feature(org, code)`, `get_limit(org, code)`, `check_limit(org, code, current)`.
- Called from the Staff, Location, Service and Booking services.
- Subscription status gates (`past_due` grace, `cancelled` → read-only).

#### UI
`/app/subscription/` shows the plan, usage bars and limits.

#### Security
Limits are enforced server-side in the services.

#### Tests
- Every limit is enforced.
- The override works.
- No `plan.slug ==` checks in code (a grep test).

#### Migration Impact
Data migration: Plan columns → PlanFeature rows. The columns are kept until M11.3.

#### Acceptance Criteria
Changing a plan's limit takes effect without a code change.

### M8.2: Usage tracking

#### Objective
Per-organization usage metering.

#### Existing Components Reused
None.

#### Models
`OrganizationUsage` (org, period_start, metric, value; unique(org, period, metric)).

#### Backend
- Counters incremented by the services (bookings created, SMS sent, API calls via middleware).
- A nightly reconciliation task.

#### UI
Usage on the subscription page and in the SaaS org detail.

#### Security
Tenant-scoped.

#### Tests
- Counter accuracy.
- Monthly rollover.

#### Migration Impact
New table.

#### Acceptance Criteria
The monthly booking limit is based on the current period's usage.

### M8.3: Billing provider abstraction

#### Objective
Stripe-ready without coupling to Stripe.

#### Existing Components Reused
`Subscription.external_*`.

#### Models
`BillingCustomer`/`BillingEvent` (webhook log, idempotent by provider event id).

#### Backend
- `billing/providers/base.py::BillingProvider` (create_customer, create_checkout, portal_url, parse_webhook, sync_subscription).
- `ManualBillingProvider` (the default).
- A `StripeBillingProvider` skeleton behind `BILLING_PROVIDER=stripe` (the `stripe` SDK is added only when this is enabled; live integration only on request).
- A webhook endpoint with signature verification.

#### UI
Upgrade and downgrade buttons, which are no-ops or manual with the manual provider.

#### Security
- Secrets come from the environment.
- Webhooks are verified and idempotent.
- No card data is ever stored.

#### Tests
- A provider contract test with a fake provider.
- Webhook idempotency.

#### Migration Impact
New tables.

#### Acceptance Criteria
Switching the provider needs configuration only. `.env.example` documents the Stripe variables as optional.

---

## Phase 9: Reporting

### M9.1: ReportingService and reports UI

#### Objective
Business reports.

#### Existing Components Reused
Dashboard selectors.

#### Models
- An optional `DailyOrgStats` rollup table (org, date, location, staff, metrics). It is populated nightly and incrementally on the booking status change.

#### Backend
Reports:
- appointment statistics;
- customer growth;
- cancellation and no-show rates;
- staff utilization (booked minutes ÷ available minutes from AvailabilityService);
- service popularity.

All are date-range and location-filtered and read from the rollups for ranges over 31 days. CSV export runs as a Celery job with a signed download link.

#### UI
`/app/reports/`: tabs and charts. Clear "insufficient data" states.

#### Security
- `reports.view`, with `FEATURE_ADVANCED_REPORTS` gating the advanced reports.
- Exports are tenant-scoped, audited and expiring.

#### Tests
- Report math.
- Export isolation.
- Query budget.

#### Migration Impact
New rollup table plus backfill command.

#### Acceptance Criteria
A 12-month report for a 50k-booking org renders in under 1 s.

---

## Phase 10: API and AI readiness

### M10.1: API v1 consolidation

#### Objective
One coherent, versioned API.

#### Existing Components Reused
All viewsets.

#### Models
None.

#### Backend
- Move auth, organizations and dashboard under `/api/v1/`, keeping the `/api/*` aliases for one deprecation cycle with a `Deprecation` header.
- Add `/api/v1/locations`, `/staff`, `/services`, `/availability?service&location&from&to&staff=any`, `/appointments` (alias of bookings), `/customers/search`, `/forms`, `/notifications`.
- Consistent error schema.
- OpenAPI tags and examples.

#### UI
None.

#### Security
The organization always comes from the auth context, never from the body.

#### Tests
- Schema generation passes.
- Contract tests for each endpoint.
- The isolation suite covers the new endpoints automatically.

#### Migration Impact
None.

#### Acceptance Criteria
`docs/API.md` matches the generated schema.

### M10.2: Service accounts, API keys and AI agent contract

#### Objective
Let external agents book safely.

#### Existing Components Reused
BookingService, AvailabilityService, CRM services.

#### Models
`ApiCredential`: org, name, hashed key, prefix, scopes (capabilities subset), `actor_type` (`integration`/`ai_agent`), last_used, revoked_at.

#### Backend
- `ApiKeyAuthentication`: the key maps to a tenant plus a capability subset.
- The `Idempotency-Key` header is required on `POST /appointments`.
- `booking_source` is derived from the credential type (`ai_agent`); the client can't set it.
- Per-key throttle.
- Everything goes through the same services, locks and constraints.

#### UI
- Settings → API keys (show the key once).
- A SaaS view of API usage.

#### Security
- Keys are stored hashed.
- Scope minimization.
- Audit `actor_type=ai_agent`.
- Gated by `FEATURE_API_ACCESS`/`FEATURE_AI_RECEPTIONIST`.

#### Tests
- An AI booking race versus a web booking (PG) → one 409.
- Replaying the idempotency key → the same response.
- Scope denial.

#### Migration Impact
New table.

#### Acceptance Criteria
`docs/AI_AGENT_INTEGRATION.md` walks through identify org → search customer → get availability → book → confirm, with example requests.

---

## Phase 11: Production hardening

### M11.1: Security, authorization and privacy review

#### Objective
Pre-production hardening.

#### Existing Components Reused
Everything.

#### Models
`DataExportRequest`, `DeletionRequest` (customer data export and anonymization jobs).

#### Backend
- HSTS, secure cookies and CSP (production settings profile).
- Upload validation (size, type, re-encode images).
- Login lockout.
- The `manage.py check --deploy` gate in CI.
- Customer export (JSON/CSV) and anonymization tasks.
- A retention settings placeholder.

#### UI
- CRM → Privacy actions.
- Portal → "download my data".

#### Security
Documented threat model. **No compliance claims are made.**

#### Tests
- Deploy check.
- Export content.
- Anonymization irreversibility.
- Upload rejection.

#### Migration Impact
New tables.

#### Acceptance Criteria
`docs/SECURITY.md` is updated with an honest status.

### M11.2: Performance, observability, accessibility

#### Objective
Operational readiness.

#### Existing Components Reused
Health and ready endpoints.

#### Models
None.

#### Backend
- Structured JSON logging with a request id and org id.
- Celery heartbeat in `/ready/`.
- Optional Sentry via an environment variable.
- Query-count tests on the key pages.
- A `select_related` audit.

#### UI
- Accessibility pass (keyboard, contrast, ARIA on modals).
- Responsive QA.

#### Security
Logs contain no PII (ids only).

#### Tests
- Query budgets.
- axe-core checks on the key templates (optional in CI).

#### Migration Impact
None.

#### Acceptance Criteria
Key pages meet their query budgets and score Lighthouse a11y ≥ 90.

### M11.3: Transitional column cleanup (requires explicit approval)

#### Objective
Remove the transitional columns kept during the earlier migrations. The legacy apps and `User.role` have already been removed in M1.6.

#### Existing Components Reused
None.

#### Models
Drop:
- `Customer.tags` JSON and `Customer.name`;
- the `Plan` limit columns;
- `BookingActivityLog`.

#### Backend
Remove the code paths that still keep these columns in sync.

#### UI
None.

#### Security
Not applicable.

#### Tests
Full suite.

#### Migration Impact
**Destructive.** Take a backup first. Each drop is a separate migration, with the rationale in `docs/DATABASE.md`.

#### Acceptance Criteria
Approved by the product owner.

---

## Cross-cutting: seed data, docs and dev environment

These are delivered incrementally, and each milestone updates them:

### `seed_demo`
- Idempotent. Uses the services, not raw ORM calls.
- Delivered from M1.5 onward, and enriched each phase.
- Creates:
  - a SaaS admin;
  - **Harmony Wellness Centre** (Downtown and North York locations; owner, manager, receptionist, massage therapist and chiropractor; 4 services; about 40 customers; past and future bookings);
  - **Serenity Spa** (1 location, its own staff and customers);
  - a pair of users with the same email in both orgs, which demonstrates isolation.
- Sends no notifications during seeding.

### Docs
Create and update as the features land:
- ARCHITECTURE
- DATABASE
- MULTI_TENANCY
- PERMISSIONS (generated matrix)
- BOOKING_ENGINE
- CRM
- API
- DEPLOYMENT
- DEVELOPMENT
- SECURITY
- SAAS_ADMIN
- SUBSCRIPTIONS
- AI_AGENT_INTEGRATION

### Dev environment
- `docker compose up --build` then `docker compose exec web python manage.py seed_demo`.
- Document a non-Docker path with a local PostgreSQL.

## Suggested commit and PR sequence

| # | Milestones |
|---|---|
| 1 | M0 |
| 2 | M1.1 + M1.2 |
| 3 | M1.3 |
| 4 | M1.4 + M1.5 |
| 5 | M1.6 |
| 6 | M2.1–M2.4 |
| 7 | M2.5 |
| 8 | M2.6 |
| 9 | M3.1–M3.3 |
| 10 | M3.4 |
| 11 | M4.1 + M4.2 |
| 12 | M4.6 |
| 13 | M4.3–M4.5 |
| 14 | M4.7 |
| 15 | M5.1–M5.4 |
| 16 | M5.5 |
| 17 | M6 |
| 18 | M7 |
| 19 | M8 |
| 20 | M9 |
| 21 | M10 |
| 22 | M11 |

**Reordered 2026-09-29:** the public booking wizard (M4.6) moves ahead of M4.3–M4.5, straight
after the availability engine and booking service (M3.4, M4.1, M4.2), because the old public
form makes customers type a time instead of choosing a free one. The wizard needs only
`AvailabilityService` and `BookingService`; the status history and calendar (M4.3–M4.5) don't
block it. M4.7 (waitlist) is split off and follows M4.3–M4.5.

## Decisions (confirmed 2026-09-27)

1. **Legacy apps:** drop the `specialists`/`appointments` apps and tables. There is **no production data**, so no export step is needed. This moves from M11.3 into **M1.6**.
2. **Test database:** **PostgreSQL** is the reference test database (CI, Docker, local). SQLite remains only as a best-effort fallback; PostgreSQL-only tests are marked `@pytest.mark.postgres`.
3. **Terminology:** keep the `Booking` model name; call it "Appointment" in the UI and docs.
4. **Customers:** phone-only customers are allowed. Email becomes optional, with an email-or-phone check constraint (M2.1).
5. **Front-end build:** Tailwind **standalone CLI** binary (Docker build plus a committed compiled CSS). No Node toolchain.
