"""Tenant resolution.

Rules (docs/MULTI_TENANCY.md):
- A tenant context exists only through an active membership of an active organization.
- A client-supplied organization slug (``X-Organization-Slug`` header or ``?organization=``)
  only *selects among the user's own memberships*; it never grants access by itself.
  If the user has no membership in the requested organization there is no context.
- Without a slug, the session's active organization is used (web UI), else the user's
  oldest membership, so the default is deterministic.
- Suspended organizations raise ``TenantSuspended`` (HTTP 403).
- Platform superusers get no implicit tenant access; platform tooling lives under /saas/.
- Public, anonymous pages resolve the organization by URL slug via ``get_public_organization``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from organizations.models import Organization, OrganizationMembership
from organizations.permissions import Capability

ORGANIZATION_HEADER = "X-Organization-Slug"
ORGANIZATION_QUERY_PARAM = "organization"
SESSION_KEY = "active_organization_id"
_CACHE_ATTR = "_tenant_context_cache"


class TenantSuspended(PermissionDenied):
    default_detail = "This organization is suspended."
    default_code = "organization_suspended"


@dataclass(frozen=True)
class TenantContext:
    organization: Organization
    membership: OrganizationMembership
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    @property
    def user(self):
        return self.membership.user

    @property
    def role(self) -> str:
        return self.membership.role

    def has(self, *capabilities: Capability | str) -> bool:
        return all(Capability(c) in self.capabilities for c in capabilities)


def requested_organization_slug(request) -> str | None:
    query_params = getattr(request, "query_params", None) or request.GET
    return request.headers.get(ORGANIZATION_HEADER) or query_params.get(ORGANIZATION_QUERY_PARAM)


def _underlying(request):
    # DRF wraps the Django HttpRequest; cache on the inner object so every layer shares it.
    return getattr(request, "_request", request)


def resolve_tenant(request) -> TenantContext | None:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None

    raw = _underlying(request)
    cache = getattr(raw, _CACHE_ATTR, None)
    if cache is not None and cache[0] == user.pk:
        return cache[1]

    context = _resolve(request, user)
    setattr(raw, _CACHE_ATTR, (user.pk, context))
    return context


def _resolve(request, user) -> TenantContext | None:
    memberships = (
        OrganizationMembership.objects.select_related("organization", "user")
        .filter(user=user, is_active=True, organization__is_active=True)
        .order_by("created_at")
    )

    slug = requested_organization_slug(request)
    if slug:
        membership = memberships.filter(organization__slug=slug).first()
    else:
        membership = None
        session = getattr(request, "session", None)
        session_org_id = session.get(SESSION_KEY) if session is not None else None
        if session_org_id:
            membership = memberships.filter(organization_id=session_org_id).first()
        if membership is None:
            membership = memberships.filter(organization__is_suspended=False).first()

    if membership is None:
        return None
    if membership.organization.is_suspended:
        raise TenantSuspended()
    return TenantContext(
        organization=membership.organization,
        membership=membership,
        capabilities=membership.capabilities,
    )


def set_active_organization(request, organization: Organization) -> None:
    """Remember the web user's chosen organization (organization switcher)."""
    request.session[SESSION_KEY] = str(organization.pk)
    raw = _underlying(request)
    if hasattr(raw, _CACHE_ATTR):
        delattr(raw, _CACHE_ATTR)


def get_public_organization(slug: str | None) -> Organization | None:
    """Organization for public, anonymous flows (booking page, public availability)."""
    if not slug:
        return None
    return Organization.objects.filter(
        slug=slug, is_active=True, is_suspended=False, booking_page_enabled=True
    ).first()


@transaction.atomic
def suspend_organization(*, organization: Organization, reason: str, actor=None) -> None:
    from core.audit import AuditAction, record_audit

    organization.is_suspended = True
    organization.suspended_at = timezone.now()
    organization.suspension_reason = reason
    organization.save(
        update_fields=["is_suspended", "suspended_at", "suspension_reason", "updated_at"]
    )
    record_audit(
        AuditAction.ORGANIZATION_SUSPENDED,
        organization=organization,
        actor=actor,
        object_type="Organization",
        object_identifier=str(organization.pk),
        metadata={"reason": reason},
    )


@transaction.atomic
def reactivate_organization(*, organization: Organization, actor=None) -> None:
    from core.audit import AuditAction, record_audit

    organization.is_suspended = False
    organization.suspended_at = None
    organization.suspension_reason = ""
    organization.save(
        update_fields=["is_suspended", "suspended_at", "suspension_reason", "updated_at"]
    )
    record_audit(
        AuditAction.ORGANIZATION_REACTIVATED,
        organization=organization,
        actor=actor,
        object_type="Organization",
        object_identifier=str(organization.pk),
    )
