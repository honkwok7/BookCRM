"""Staff reads, always scoped to one organization."""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet
from django.db.models.functions import Upper

from locations.models import Location
from services.selectors import service_offered_at
from staff.models import StaffProfile, StaffServiceOffering


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
