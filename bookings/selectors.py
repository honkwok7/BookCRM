from __future__ import annotations

from django.db.models import Q, QuerySet

from bookings.models import Booking
from organizations.models import OrganizationRole
from organizations.permissions import Capability
from organizations.tenancy import (
    get_public_organization,
    requested_organization_slug,
    resolve_tenant,
)


def _own_customer_filter(user) -> Q:
    """Bookings that belong to ``user`` as a customer.

    Matching by email is only trusted once the account's email is verified; otherwise anyone
    could register with somebody else's address and read their bookings.
    """
    condition = Q(customer__user=user)
    if user.email_verified:
        condition |= Q(customer_email__iexact=user.email)
    return condition


def bookings_visible_to(request) -> QuerySet[Booking]:
    queryset = Booking.objects.select_related("organization", "service", "staff", "customer")
    user = request.user
    if not user.is_authenticated:
        return queryset.none()

    tenant = resolve_tenant(request)
    if tenant is not None:
        queryset = queryset.filter(organization=tenant.organization)
        if tenant.has(Capability.APPOINTMENTS_VIEW_ALL):
            return queryset
        if tenant.role == OrganizationRole.STAFF:
            return queryset.filter(staff__user=user)
        return queryset.filter(_own_customer_filter(user))

    # No membership in the requested (or any) organization: the user's own customer bookings,
    # optionally narrowed to one public organization.
    slug = requested_organization_slug(request)
    if slug:
        organization = get_public_organization(slug)
        if organization is None:
            return queryset.none()
        queryset = queryset.filter(organization=organization)
    return queryset.filter(_own_customer_filter(user))
