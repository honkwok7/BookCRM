"""Read-only audit log API for organization owners (capability ``audit.view``)."""

from rest_framework import permissions, serializers, viewsets

from core.models import AuditLog
from core.permissions import HasCapability
from organizations.selectors import scope_queryset_by_organization


class AuditLogSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True, default=None)

    class Meta:
        model = AuditLog
        fields = (
            "id",
            "created_at",
            "action",
            "actor_type",
            "user",
            "user_email",
            "impersonator",
            "object_type",
            "object_identifier",
            "metadata",
            "ip_address",
        )
        read_only_fields = fields


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditLogSerializer
    permission_classes = [permissions.IsAuthenticated, HasCapability(read="audit.view")]
    ordering_fields = ("created_at",)
    FILTERS = ("action", "object_type", "object_identifier", "actor_type")

    def get_queryset(self):
        queryset = scope_queryset_by_organization(
            AuditLog.objects.select_related("user"), self.request
        )
        for name in self.FILTERS:
            value = self.request.query_params.get(name)
            if value:
                queryset = queryset.filter(**{name: value})
        return queryset
