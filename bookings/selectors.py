from __future__ import annotations

from django.db.models import Exists, OuterRef, Q, QuerySet

from bookings.models import Booking
from organizations.models import OrganizationRole
from organizations.permissions import Capability
from organizations.tenancy import (
    get_public_organization,
    requested_organization_slug,
    resolve_tenant,
)
from services.selectors import services_bookable_at
from staff.models import StaffProfile


def bookable_services(organization, *, public: bool, location=None):
    """Services that can be booked now; the public only sees those marked public."""
    return services_bookable_at(organization, location, public=public)


def bookable_staff(organization, *, public: bool = True):
    """Providers that can be booked now; the public page lists only those visible online.

    Which services each provider offers, and where, is ``staff.selectors.list_providers_for``;
    the booking engine starts enforcing it with the availability rewrite (M3.4/M4.1).
    """
    staff = StaffProfile.objects.filter(
        organization=organization, is_active=True, is_accepting_bookings=True
    ).select_related("user")
    return staff.filter(online_booking_visible=True) if public else staff


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


def bookings_for_customer_account(user) -> QuerySet[Booking]:
    """Every booking that belongs to ``user`` as a customer, across organizations (portal)."""
    if not user.is_authenticated:
        return Booking.objects.none()
    return Booking.objects.select_related("organization", "service", "staff__user").filter(
        _own_customer_filter(user)
    )


def overlapping_bookings() -> QuerySet[Booking]:
    """Active appointments that overlap another active appointment of the same staff member.

    Should always be empty: the booking service refuses overlaps and, on PostgreSQL, the
    ``booking_staff_no_overlap`` constraint makes them impossible. Used before adding that
    constraint (``manage.py check_booking_overlaps``).
    """
    from bookings.services import ACTIVE_BOOKING_STATUSES

    active = Booking.objects.filter(status__in=ACTIVE_BOOKING_STATUSES)
    clash = active.filter(
        staff_id=OuterRef("staff_id"),
        start_datetime__lt=OuterRef("end_datetime"),
        end_datetime__gt=OuterRef("start_datetime"),
    ).exclude(pk=OuterRef("pk"))
    return (
        active.filter(Exists(clash))
        .select_related("organization", "staff__user")
        .order_by("organization__name", "staff_id", "start_datetime")
    )
