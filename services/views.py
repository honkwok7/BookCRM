import django_filters
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from core.api import AuditedModelViewSetMixin
from core.audit import AuditAction
from core.filters import TenantModelChoiceFilter
from core.permissions import HasCapability
from locations.models import Location
from organizations.selectors import scope_queryset_by_organization
from services.models import Service, ServiceCategory
from services.serializers import ServiceCategorySerializer, ServiceSerializer
from staff.selectors import list_providers_for
from staff.serializers import StaffProfileSerializer


class ServiceCategoryViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    audit_actions = {
        "create": AuditAction.SERVICE_CATEGORY_CREATED,
        "update": AuditAction.SERVICE_CATEGORY_UPDATED,
        "delete": AuditAction.SERVICE_CATEGORY_DELETED,
    }
    serializer_class = ServiceCategorySerializer
    permission_classes = [
        permissions.IsAuthenticated,
        HasCapability(read="services.view", write="services.manage"),
    ]

    def get_queryset(self):
        queryset = ServiceCategory.objects.select_related("organization")
        return scope_queryset_by_organization(queryset, self.request)


class ServiceFilter(django_filters.FilterSet):
    category = TenantModelChoiceFilter(ServiceCategory)

    class Meta:
        model = Service
        fields = ("is_active", "is_public", "is_archived", "category")


class ServiceViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    audit_actions = {
        "create": AuditAction.SERVICE_CREATED,
        "update": AuditAction.SERVICE_UPDATED,
        "delete": AuditAction.SERVICE_DELETED,
    }
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
            "assigned_staff_members"
        )
        return scope_queryset_by_organization(queryset, self.request)

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
