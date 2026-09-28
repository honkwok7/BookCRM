from rest_framework import permissions, viewsets

from core.permissions import IsOrganizationManagerOrOwner
from organizations.selectors import scope_queryset_by_organization
from services.models import Service, ServiceCategory
from services.serializers import ServiceCategorySerializer, ServiceSerializer


class ServiceCategoryViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceCategorySerializer
    permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]

    def get_queryset(self):
        queryset = ServiceCategory.objects.select_related("organization")
        return scope_queryset_by_organization(queryset, self.request)


class ServiceViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]
    filterset_fields = ("is_active", "is_public", "is_archived", "category")
    search_fields = ("name", "description")
    ordering_fields = ("name", "price", "duration_minutes", "created_at")

    def get_queryset(self):
        queryset = Service.objects.select_related("organization", "category").prefetch_related(
            "assigned_staff_members"
        )
        return scope_queryset_by_organization(queryset, self.request)
