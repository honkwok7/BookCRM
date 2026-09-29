from __future__ import annotations

from django.conf import settings
from django.contrib.messages import get_messages

from core.navigation import navigation_for
from core.web import TEAM_ROLES
from organizations.models import OrganizationMembership
from organizations.tenancy import TenantSuspended, resolve_tenant


def app_shell(request) -> dict:
    """Layout data: the tenant, the capability-filtered sidebar and the organization switcher.

    Error pages render through this too, so it never raises (a suspended organization simply
    has no tenant here; the page itself returns 403).
    """
    context = {"brand_name": settings.SITE_NAME, "toast_messages": lambda: _toasts(request)}
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return context

    try:
        tenant = resolve_tenant(request)
    except TenantSuspended:
        tenant = None
    match = getattr(request, "resolver_match", None)
    memberships = list(
        OrganizationMembership.objects.filter(
            user=user, is_active=True, organization__is_active=True
        )
        .select_related("organization")
        .order_by("organization__name")
    )
    context.update(
        tenant=tenant,
        is_team_member=tenant is not None and tenant.role in TEAM_ROLES,
        nav_sections=navigation_for(tenant, match.url_name if match else None) if tenant else [],
        memberships=memberships,
    )
    return context


def _toasts(request) -> list[dict]:
    """Django messages as toast data (templates/components/toasts.html). Reading consumes them."""
    return [
        {"message": str(message), "level": message.level_tag or "info"}
        for message in get_messages(request)
    ]
