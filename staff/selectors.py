"""Staff reads, always scoped to one organization."""

from __future__ import annotations

from django.db.models import Prefetch, Q, QuerySet

from locations.models import Location
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


def list_providers_for(service, location=None, *, public: bool = False):
    """Staff who can be booked for ``service`` (at ``location``, when given).

    A provider qualifies when they are active and accepting bookings, have an active offering
    of the service that applies there, and (with a location) work at that location. ``public``
    also requires ``online_booking_visible``: hidden providers are bookable by the team only.
    """
    offerings = StaffServiceOffering.objects.filter(_offering_filter(service, location))
    providers = StaffProfile.objects.filter(
        organization_id=service.organization_id,
        is_active=True,
        is_accepting_bookings=True,
        pk__in=offerings.values("staff_id"),
    )
    if location is not None:
        providers = providers.filter(locations=location)
    if public:
        providers = providers.filter(online_booking_visible=True)
    return providers.select_related("user").distinct()


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
