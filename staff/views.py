from rest_framework import permissions, viewsets

from core.api import AuditedModelViewSetMixin
from core.audit import AuditAction
from core.permissions import HasCapability
from organizations.selectors import scope_queryset_by_organization
from staff.models import StaffProfile
from staff.serializers import StaffProfileSerializer


class StaffProfileViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    audit_actions = {
        "create": AuditAction.STAFF_CREATED,
        "update": AuditAction.STAFF_UPDATED,
        "delete": AuditAction.STAFF_DELETED,
    }
    audit_redact_fields = ("phone_number",)
    serializer_class = StaffProfileSerializer
    permission_classes = [
        permissions.IsAuthenticated,
        HasCapability(read="staff.view", write="staff.manage"),
    ]
    filterset_fields = ("is_active", "is_accepting_bookings")
    search_fields = ("user__email", "job_title", "bio")

    def get_queryset(self):
        queryset = StaffProfile.objects.select_related("user", "organization")
        return scope_queryset_by_organization(queryset, self.request)
