import django_filters
from rest_framework import permissions, viewsets

from core.api import AuditedModelViewSetMixin
from core.filters import TenantModelChoiceFilter
from core.permissions import HasCapability
from locations.models import Location
from organizations.selectors import get_request_organization
from services.models import Service
from staff.models import StaffProfile, StaffServiceOffering
from staff.selectors import staff_for
from staff.serializers import StaffProfileSerializer, StaffServiceOfferingSerializer
from staff.services import delete_staff_profile, remove_offering

STAFF_ACCESS = HasCapability(read="staff.view", write="staff.manage")


class StaffProfileFilter(django_filters.FilterSet):
    location = TenantModelChoiceFilter(Location, field_name="locations")

    class Meta:
        model = StaffProfile
        fields = ("is_active", "is_accepting_bookings", "online_booking_visible", "location")


class StaffProfileViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Providers. Read: ``staff.view``. Write: ``staff.manage``.

    Creating one needs an active team member (``user``) and counts against the plan's staff
    limit (409 ``plan_limit``). ``locations`` replaces where they work; offerings tied to a
    dropped location are removed. Someone with appointments can't be deleted (409
    ``in_use``): deactivate them instead.
    """

    audited_by_service = ("create", "update", "delete")
    serializer_class = StaffProfileSerializer
    permission_classes = [permissions.IsAuthenticated, STAFF_ACCESS]
    filterset_class = StaffProfileFilter
    search_fields = ("user__email", "user__first_name", "user__last_name", "display_name")
    ordering_fields = ("created_at", "user__first_name")

    def get_queryset(self):
        organization = get_request_organization(self.request)
        if organization is None:
            return StaffProfile.objects.none()
        return staff_for(organization).select_related("organization").distinct()

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        delete_staff_profile(staff=instance, actor=self.request.user)


class StaffServiceOfferingFilter(django_filters.FilterSet):
    staff = TenantModelChoiceFilter(StaffProfile)
    service = TenantModelChoiceFilter(Service)
    location = TenantModelChoiceFilter(Location)

    class Meta:
        model = StaffServiceOffering
        fields = ("staff", "service", "location", "is_active")


class StaffServiceOfferingViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Who offers which service where. An empty ``location`` means "at every location this
    person works at". ``custom_duration_minutes`` and ``custom_price`` override the service's
    own; ``duration_minutes`` and ``price`` are the values that apply."""

    audited_by_service = ("create", "update", "delete")
    serializer_class = StaffServiceOfferingSerializer
    permission_classes = [permissions.IsAuthenticated, STAFF_ACCESS]
    filterset_class = StaffServiceOfferingFilter
    search_fields = ("service__name",)
    ordering_fields = ("created_at",)

    def get_queryset(self):
        organization = get_request_organization(self.request)
        if organization is None:
            return StaffServiceOffering.objects.none()
        return StaffServiceOffering.objects.filter(organization=organization).select_related(
            "service", "staff", "location"
        )

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        remove_offering(offering=instance, actor=self.request.user)
