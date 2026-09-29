"""Service reads, always scoped to one organization."""

from __future__ import annotations

from django.db.models import Count, Prefetch, Q, QuerySet

from locations.models import Location
from services.models import Service, ServiceCategory


def services_for(organization) -> QuerySet[Service]:
    return (
        Service.objects.filter(organization=organization)
        .select_related("category")
        .prefetch_related(Prefetch("locations", queryset=Location.objects.order_by("name")))
    )


def categories_for(organization) -> QuerySet[ServiceCategory]:
    return ServiceCategory.objects.filter(organization=organization).annotate(
        service_count=Count("services", filter=Q(services__is_archived=False))
    )


def offered_at_filter(location) -> Q:
    """Services offered at ``location``: those limited to it, or not limited at all."""
    return Q(locations=location) | Q(locations__isnull=True)


def service_offered_at(service: Service, location) -> bool:
    locations = service.locations.all()
    if hasattr(service, "_prefetched_objects_cache") and "locations" in (
        service._prefetched_objects_cache
    ):
        return not locations or any(item.pk == location.pk for item in locations)
    return not locations.exists() or locations.filter(pk=location.pk).exists()


def services_bookable_at(organization, location=None, *, public: bool):
    """Services that can be booked now (at ``location``). The public only sees services
    marked bookable online."""
    services = Service.objects.filter(organization=organization, is_active=True, is_archived=False)
    if public:
        services = services.filter(is_public=True)
    if location is not None:
        services = services.filter(offered_at_filter(location)).distinct()
    return services
