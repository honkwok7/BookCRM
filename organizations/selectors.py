from __future__ import annotations

from collections.abc import Iterable

from organizations.models import Organization, OrganizationMembership


def get_request_organization(request) -> Organization | None:
    slug = request.headers.get("X-Organization-Slug") or request.query_params.get("organization")
    if slug:
        try:
            return Organization.objects.get(slug=slug, is_active=True)
        except Organization.DoesNotExist:
            return None

    if request.user.is_authenticated:
        membership = (
            OrganizationMembership.objects.select_related("organization")
            .filter(user=request.user, is_active=True, organization__is_active=True)
            .first()
        )
        if membership:
            return membership.organization
    return None


def user_has_org_role(user, organization: Organization, roles: Iterable[str] | None = None) -> bool:
    if user.is_superuser:
        return True
    queryset = OrganizationMembership.objects.filter(
        user=user,
        organization=organization,
        is_active=True,
    )
    if roles:
        queryset = queryset.filter(role__in=roles)
    return queryset.exists()


def scope_queryset_by_organization(queryset, request):
    if request.user.is_superuser:
        return queryset
    organization = get_request_organization(request)
    if organization is None:
        return queryset.none()
    return queryset.filter(organization=organization)
