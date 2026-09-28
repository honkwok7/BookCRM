from django.db.models import Count, Q
from rest_framework import permissions, viewsets

from core.api import AuditedModelViewSetMixin
from core.permissions import HasCapability
from crm.models import Tag
from crm.serializers import TagSerializer
from crm.services import delete_tag
from organizations.selectors import scope_queryset_by_organization


class TagViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Organization-defined customer tags. Writes go through crm.services (audited there)."""

    audited_by_service = ("create", "update", "delete")
    serializer_class = TagSerializer
    permission_classes = [
        permissions.IsAuthenticated,
        HasCapability(read="customers.view", write="customers.manage"),
    ]
    search_fields = ("name",)
    ordering_fields = ("name", "created_at")

    def get_queryset(self):
        queryset = Tag.objects.select_related("organization").annotate(
            customer_count=Count(
                "customer_tags", filter=~Q(customer_tags__customer__status="anonymized")
            )
        )
        return scope_queryset_by_organization(queryset, self.request)

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        delete_tag(tag=instance, actor=self.request.user)
