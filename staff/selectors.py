"""Staff reads, always scoped to one organization."""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet
from django.db.models.functions import Upper

from locations.models import Location
from organizations.models import OrganizationRole
from services.selectors import service_offered_at
from staff.models import StaffProfile, StaffServiceOffering

_UNSET = object()


def own_profile(tenant) -> StaffProfile | None:
    """The signed-in member's active staff profile in this organization, if they take
    appointments (any role: an owner can be a provider too). Cached on the membership for the
    request, since the sidebar asks as well as the page."""
    membership = tenant.membership
    cached = getattr(membership, "_own_staff_profile", _UNSET)
    if cached is _UNSET:
        cached = (
            StaffProfile.objects.filter(
                organization=tenant.organization, user=tenant.user, is_active=True
            )
            .select_related("user", "organization")
            .first()
        )
        membership._own_staff_profile = cached
    return cached


def is_provider_here(tenant) -> bool:
    if tenant is None:
        return False
    known = getattr(tenant.membership, "has_staff_profile", None)  # see organizations.tenancy
    return known if known is not None else own_profile(tenant) is not None


def can_open_provider_day(tenant) -> bool:
    """``/staff/dashboard/``: providers, and staff-role members without a profile yet (it is
    where they land; they get an empty state)."""
    return is_provider_here(tenant) or (
        tenant is not None and tenant.role == OrganizationRole.STAFF
    )


def staff_for(organization) -> QuerySet[StaffProfile]:
    return (
        StaffProfile.objects.filter(organization=organization)
        .select_related("user")
        .prefetch_related(
            Prefetch("locations", queryset=Location.objects.order_by("-is_default", "name"))
        )
    )


def _offering_filter(service, location) -> Q:
    """Active offerings of ``service`` that apply at ``location`` (any location if None)."""
    condition = Q(service=service, is_active=True)
    if location is not None:
        condition &= Q(location=location) | Q(location__isnull=True)
    return condition


def with_providers(services, location, *, public: bool = False):
    """``services`` (a queryset) narrowed to those at least one provider can be booked for at
    ``location``: the same rule as ``list_providers_for``, but one query for all of them.
    The caller has already limited ``services`` to those offered at ``location``."""
    offerings = (
        StaffServiceOffering.objects.filter(service=OuterRef("pk"), is_active=True)
        .filter(Q(location=location) | Q(location__isnull=True))
        .filter(
            staff__is_active=True,
            staff__is_accepting_bookings=True,
            staff__locations=location,
        )
        .annotate(provider_type=Upper("staff__provider_type"))
        .filter(
            Q(service__required_provider_type="")
            | Q(provider_type=Upper(OuterRef("required_provider_type")))
        )
    )
    if public:
        offerings = offerings.filter(staff__online_booking_visible=True)
    return services.filter(Exists(offerings))


def list_providers_for(service, location=None, *, public: bool = False):
    """Staff who can be booked for ``service`` (at ``location``, when given).

    A provider qualifies when they are active and accepting bookings, have an active offering
    of the service that applies there, have the service's required provider type (if any),
    and (with a location) work at that location, where the service must be offered. ``public``
    also requires ``online_booking_visible``: hidden providers are bookable by the team only.
    """
    if location is not None and not service_offered_at(service, location):
        return StaffProfile.objects.none()
    offerings = StaffServiceOffering.objects.filter(_offering_filter(service, location))
    providers = StaffProfile.objects.filter(
        organization_id=service.organization_id,
        is_active=True,
        is_accepting_bookings=True,
        pk__in=offerings.values("staff_id"),
    )
    if location is not None:
        providers = providers.filter(locations=location)
    if service.required_provider_type:
        providers = providers.filter(provider_type__iexact=service.required_provider_type)
    if public:
        providers = providers.filter(online_booking_visible=True)
    return providers.select_related("user").distinct()


def provides(staff, service, location=None, *, public: bool = False) -> bool:
    """Can ``staff`` be booked for ``service`` (at ``location``)? The same rule as
    ``list_providers_for``."""
    return list_providers_for(service, location, public=public).filter(pk=staff.pk).exists()


def offering_for(staff, service, location=None) -> StaffServiceOffering | None:
    """The offering that applies: a location-specific one wins over "all locations"."""
    offerings = StaffServiceOffering.objects.filter(
        _offering_filter(service, location), staff=staff
    ).select_related("service")
    if location is not None and not staff.locations.filter(pk=location.pk).exists():
        return None
    rows = list(offerings)
    return min(rows, key=lambda offering: offering.location_id is None) if rows else None


def offerings_by_service(staff) -> dict:
    """``{service_id: [offering, ...]}`` for the staff member's offerings."""
    grouped: dict = {}
    for offering in staff.offerings.select_related("service", "location"):
        grouped.setdefault(offering.service_id, []).append(offering)
    return grouped
