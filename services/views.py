import django_filters
from rest_framework import permissions, viewsets

from core.api import AuditedModelViewSetMixin
from core.audit import AuditAction
from core.filters import TenantModelChoiceFilter
from core.permissions import HasCapability
from organizations.selectors import scope_queryset_by_organization
from services.models import Service, ServiceCategory
from services.serializers import ServiceCategorySerializer, ServiceSerializer


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
