# Multi-Tenancy

The tenant boundary is the **`Organization`**. Every tenant-owned row has an `organization`
foreign key, and every query that serves a tenant is filtered by the organization resolved
for the current request.

## How a request's organization is resolved

`organizations/tenancy.py::resolve_tenant(request)` returns a `TenantContext`
(`organization`, `membership`, `capabilities`) or `None`.

| Rule | Behaviour |
|---|---|
| Membership required | A context exists only through an **active membership** of an **active** organization. |
| Client-supplied slug | `X-Organization-Slug` header or `?organization=` **only selects among the user's own memberships**. A slug for an organization the user doesn't belong to gives **no context** (403). It never falls back to another organization. |
| No slug | The session's active organization (web UI, `set_active_organization`), otherwise the user's **oldest** membership that isn't suspended, so the default is deterministic. |
| Suspended organization | Explicitly selecting a suspended organization raises `TenantSuspended` → HTTP 403 `"This organization is suspended."` |
| Platform superusers | **No implicit tenant access.** A superuser without a membership gets no context. Platform tooling will live under `/saas/` (M5.5) with audited impersonation. |
| Caching | The context is computed once per request and cached on the underlying `HttpRequest`. |

Public, anonymous flows (booking page, public slot search) use
`get_public_organization(slug)`, which only returns active, non-suspended organizations
with `booking_page_enabled=True`. It never creates a tenant context.

## Using it in code

- **Permissions:** `core.permissions.HasCapability(read=..., write=...)` resolves the tenant
  and checks capabilities. `IsOrganizationMember` requires any membership. See
  [PERMISSIONS.md](PERMISSIONS.md).
- **Querysets:** `organizations.selectors.scope_queryset_by_organization(queryset, request)`.
  No context means an empty queryset.
- **Creating rows:** serializers set `organization` from the tenant (`get_request_organization`),
  never from request data. `organization` is read-only in every serializer.
- **Object-level scoping** that depends on the role lives in selectors, for example
  `bookings.selectors.bookings_visible_to(request)`:
  - `appointments.view_all` → all of the organization's appointments.
  - Staff → only appointments assigned to them.
  - Customers → only their own appointments: linked `Customer.user`, or matching email **only if
    the account's email is verified**.

## Suspension

`suspend_organization(organization=, reason=, actor=)` and `reactivate_organization(...)`
set `is_suspended`, `suspended_at` and `suspension_reason`, and write an audit log entry.
Tenant users can't change `is_active` or `is_suspended` through the API.

## Writing relations safely

Serializers for tenant data extend `core.api.TenantScopedModelSerializer`:
- `organization` is set from the tenant on create.
- Every auto-generated relation field is a `TenantPrimaryKeyRelatedField`, limited to the
  tenant's rows.
- Relations to users use `TenantMemberUserField`, which only accepts members of the
  organization.
- `core/test_api_contract.py` fails if a serializer uses `fields = "__all__"`, or has a
  writable relation that isn't tenant-limited.

## Known gaps (tracked in IMPLEMENTATION_PLAN.md)

- **Web session tenant switching (M2.5):** the `/app/switch/<slug>/` view arrives with the web
  app shell. `set_active_organization` is already in place.
- **Filter id checks:** `django-filter` filters on relation ids (`?service=`, `?staff=`) validate
  ids against all organizations. The results are still tenant-scoped; only the "invalid choice"
  message differs. This is revisited with the API consolidation in M10.1.
