import django_filters
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from core.api import AuditedModelViewSetMixin
from core.filters import TenantModelChoiceFilter
from core.permissions import HasCapability
from locations.models import Location
from organizations.selectors import scope_queryset_by_organization
from services.models import Service, ServiceCategory
from services.selectors import offered_at_filter
from services.serializers import ServiceCategorySerializer, ServiceSerializer
from services.services import delete_category, delete_service
from staff.selectors import list_providers_for
from staff.serializers import StaffProfileSerializer


class ServiceCategoryViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Service categories, ordered by ``sort_order`` then name. Writes go through
    services.services (unique names and slugs, audited there)."""

    audited_by_service = ("create", "update", "delete")
    serializer_class = ServiceCategorySerializer
    permission_classes = [
        permissions.IsAuthenticated,
        HasCapability(read="services.view", write="services.manage"),
    ]

    def get_queryset(self):
        queryset = ServiceCategory.objects.select_related("organization")
        return scope_queryset_by_organization(queryset, self.request)

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        delete_category(category=instance, actor=self.request.user)


class ServiceFilter(django_filters.FilterSet):
    category = TenantModelChoiceFilter(ServiceCategory)
    # Services offered at this location (limited to it, or not limited at all).
    location = TenantModelChoiceFilter(Location, method="filter_location")

    class Meta:
        model = Service
        fields = ("is_active", "is_public", "is_archived", "category")

    def filter_location(self, queryset, name, location):
        return queryset.filter(offered_at_filter(location)).distinct()


class ServiceViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Services. Read: ``services.view``. Write: ``services.manage``.

    ``locations`` empty means every location. Creating or unarchiving beyond the plan's service
    limit gives 409 ``plan_limit``; deleting a service with appointments gives 409 ``in_use``
    (archive it instead).
    """

    audited_by_service = ("create", "update", "delete")
    serializer_class = ServiceSerializer
    permission_classes = [
        permissions.IsAuthenticated,
        HasCapability(read="services.view", write="services.manage"),
    ]
    filterset_class = ServiceFilter
    search_fields = ("name", "description")
    ordering_fields = ("name", "price", "duration_minutes", "created_at")

    def get_queryset(self):
        queryset = Service.objects.select_related("organization", "category").prefetch_related(
            "assigned_staff_members", "locations"
        )
        return scope_queryset_by_organization(queryset, self.request)

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        delete_service(service=instance, actor=self.request.user)

    @extend_schema(
        parameters=[OpenApiParameter("location", str, description="Location id (optional)")],
        responses=StaffProfileSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def providers(self, request, pk=None):
        """Staff who can be booked for this service, optionally at ``?location=<id>``."""
        service = self.get_object()
        location = None
        location_id = request.query_params.get("location")
        if location_id:
            try:
                location = Location.objects.filter(
                    organization=service.organization, pk=location_id
                ).first()
            except ValueError, DjangoValidationError:
                location = None
            if location is None:
                raise ValidationError({"location": "Location not found."})
        providers = list_providers_for(service, location).prefetch_related("locations")
        return Response(
            StaffProfileSerializer(providers, many=True, context={"request": request}).data
        )
