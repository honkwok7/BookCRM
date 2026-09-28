from rest_framework import permissions, viewsets

from core.permissions import HasCapability
from organizations.selectors import scope_queryset_by_organization
from staff.models import StaffProfile
from staff.serializers import StaffProfileSerializer


class StaffProfileViewSet(viewsets.ModelViewSet):
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
