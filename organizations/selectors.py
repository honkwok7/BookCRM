from __future__ import annotations

from organizations.models import Organization
from organizations.tenancy import resolve_tenant


def get_request_organization(request) -> Organization | None:
    """The organization of the request's tenant context (membership-backed), or None."""
    context = resolve_tenant(request)
    return context.organization if context else None


def scope_queryset_by_organization(queryset, request):
    """Filter ``queryset`` to the request's tenant. No tenant context means no rows."""
    organization = get_request_organization(request)
    if organization is None:
        return queryset.none()
    return queryset.filter(organization=organization)
