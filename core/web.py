"""Access control and htmx helpers for server-rendered pages.

Every page under /app/ and /staff/ inherits ``TenantPageMixin``: it signs the user in, resolves
the tenant exactly as the API does (organizations/tenancy.py) and checks capabilities, never
role names. Hiding a navigation link is only cosmetic; these checks are what deny access.
"""

from __future__ import annotations

import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.cache import patch_vary_headers

from organizations.models import OrganizationRole
from organizations.permissions import Capability
from organizations.tenancy import TenantContext, TenantSuspended, resolve_tenant

# Roles that work inside an organization (everyone except customers, who use /portal/).
TEAM_ROLES = frozenset(
    {
        OrganizationRole.OWNER,
        OrganizationRole.MANAGER,
        OrganizationRole.RECEPTIONIST,
        OrganizationRole.STAFF,
    }
)

SUSPENDED_MESSAGE = "This organization is suspended. Contact support to restore access."
NO_ORGANIZATION_MESSAGE = "You don't belong to an organization yet."
NO_ACCESS_MESSAGE = "You don't have permission to view this page."


def organization_zone(organization) -> ZoneInfo:
    try:
        return ZoneInfo(organization.timezone)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def tenant_for_page(request) -> TenantContext | None:
    """The request's tenant; a suspended organization is a 403 page, not an API error."""
    try:
        return resolve_tenant(request)
    except TenantSuspended as error:
        raise PermissionDenied(SUSPENDED_MESSAGE) from error


def require_capabilities(tenant: TenantContext, capabilities) -> None:
    if capabilities and not tenant.has(*capabilities):
        raise PermissionDenied(NO_ACCESS_MESSAGE)


class TenantPageMixin(LoginRequiredMixin):
    """A page inside the organization app, for team members with ``required_capabilities``."""

    required_capabilities: tuple[Capability, ...] = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        tenant = tenant_for_page(request)
        if tenant is None:
            raise PermissionDenied(NO_ORGANIZATION_MESSAGE)
        if tenant.role not in TEAM_ROLES:
            raise PermissionDenied(NO_ACCESS_MESSAGE)
        require_capabilities(tenant, self.required_capabilities)
        self.tenant = tenant
        # Dates on the page render in the organization's time zone. The template renders after
        # dispatch returns; core.middleware.ResetTimezoneMiddleware deactivates it afterwards.
        timezone.activate(organization_zone(tenant.organization))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {"organization": self.tenant.organization}


class PlatformAdminMixin(LoginRequiredMixin):
    """Platform (SaaS operator) pages. Superusers only; they get no tenant access from this."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not request.user.is_superuser:
            raise PermissionDenied(NO_ACCESS_MESSAGE)
        return super().dispatch(request, *args, **kwargs)


def is_htmx(request) -> bool:
    """An htmx request for part of a page. History restores (back button) need the full page."""
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )


class HtmxPartialMixin:
    """Render only the ``partial_name`` partial of the page for htmx requests.

    Search, filter and pagination controls target ``#<partial_name>`` and push the URL, so the
    same URL serves the full page (direct visit, reload) and the fragment (htmx).
    """

    partial_name = "results"

    def get_template_names(self):
        names = super().get_template_names()
        if is_htmx(self.request):
            return [f"{name}#{self.partial_name}" for name in names]
        return names

    def render_to_response(self, context, **response_kwargs):
        response = super().render_to_response(context, **response_kwargs)
        patch_vary_headers(response, ("HX-Request",))
        return response


def htmx_trigger(response, event: str, detail=None):
    """Add an HX-Trigger event, e.g. ``htmx_trigger(response, "toast", {"message": "Saved"})``."""
    events = json.loads(response.headers.get("HX-Trigger", "{}") or "{}")
    events[event] = detail if detail is not None else True
    response.headers["HX-Trigger"] = json.dumps(events)
    return response


def toast(response, message: str, level: str = "success"):
    return htmx_trigger(response, "toast", {"message": message, "level": level})
