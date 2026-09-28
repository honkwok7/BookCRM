from datetime import datetime

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.api import AuditedModelViewSetMixin
from core.audit import AuditAction
from core.permissions import HasCapability
from organizations.selectors import scope_queryset_by_organization
from organizations.tenancy import get_public_organization, requested_organization_slug
from scheduling.models import (
    AvailabilityException,
    OrganizationHoliday,
    TimeOff,
    WeeklyAvailability,
)
from scheduling.serializers import (
    AvailabilityExceptionSerializer,
    OrganizationHolidaySerializer,
    TimeOffSerializer,
    WeeklyAvailabilitySerializer,
)
from scheduling.services import generate_slots
from services.models import Service
from staff.models import StaffProfile

SCHEDULE_PERMISSION = HasCapability(read="staff.view", write="staff.manage")


class WeeklyAvailabilityViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    audit_actions = {
        "create": AuditAction.AVAILABILITY_CREATED,
        "update": AuditAction.AVAILABILITY_UPDATED,
        "delete": AuditAction.AVAILABILITY_DELETED,
    }
    serializer_class = WeeklyAvailabilitySerializer
    permission_classes = [permissions.IsAuthenticated, SCHEDULE_PERMISSION]

    def get_queryset(self):
        queryset = WeeklyAvailability.objects.select_related("organization", "staff")
        return scope_queryset_by_organization(queryset, self.request)


class AvailabilityExceptionViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    audit_actions = {
        "create": AuditAction.AVAILABILITY_EXCEPTION_CREATED,
        "update": AuditAction.AVAILABILITY_EXCEPTION_UPDATED,
        "delete": AuditAction.AVAILABILITY_EXCEPTION_DELETED,
    }
    serializer_class = AvailabilityExceptionSerializer
    permission_classes = [permissions.IsAuthenticated, SCHEDULE_PERMISSION]

    def get_queryset(self):
        queryset = AvailabilityException.objects.select_related("organization", "staff")
        return scope_queryset_by_organization(queryset, self.request)


class TimeOffViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    audit_actions = {
        "create": AuditAction.TIME_OFF_CREATED,
        "update": AuditAction.TIME_OFF_UPDATED,
        "delete": AuditAction.TIME_OFF_DELETED,
    }
    serializer_class = TimeOffSerializer
    permission_classes = [permissions.IsAuthenticated, SCHEDULE_PERMISSION]

    def get_queryset(self):
        queryset = TimeOff.objects.select_related("organization", "staff")
        return scope_queryset_by_organization(queryset, self.request)


class OrganizationHolidayViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    audit_actions = {
        "create": AuditAction.HOLIDAY_CREATED,
        "update": AuditAction.HOLIDAY_UPDATED,
        "delete": AuditAction.HOLIDAY_DELETED,
    }
    serializer_class = OrganizationHolidaySerializer
    permission_classes = [permissions.IsAuthenticated, SCHEDULE_PERMISSION]

    def get_queryset(self):
        queryset = OrganizationHoliday.objects.select_related("organization")
        return scope_queryset_by_organization(queryset, self.request)


class SlotViewSet(viewsets.ViewSet):
    permission_classes = [permissions.AllowAny]

    @action(detail=False, methods=["get"], url_path="available-slots")
    def available_slots(self, request):
        # Public endpoint: the organization comes from the public booking slug, never a membership.
        org = get_public_organization(requested_organization_slug(request))
        if not org:
            return Response({"detail": "Organization not found"}, status=404)

        service_id = request.query_params.get("service")
        staff_id = request.query_params.get("staff")
        date_value = request.query_params.get("date")
        if not (service_id and staff_id and date_value):
            return Response({"detail": "service, staff and date are required"}, status=400)

        try:
            date_obj = datetime.strptime(date_value, "%Y-%m-%d").date()
            service = Service.objects.filter(
                id=service_id, organization=org, is_active=True, is_public=True, is_archived=False
            ).first()
            staff_profile = StaffProfile.objects.filter(
                id=staff_id, organization=org, is_active=True, is_accepting_bookings=True
            ).first()
        except ValueError, DjangoValidationError:
            return Response({"detail": "Invalid service, staff or date"}, status=400)
        if service is None or staff_profile is None:
            return Response({"detail": "Service or staff not found"}, status=404)

        slots = generate_slots(
            organization=org, service=service, staff_profile=staff_profile, date=date_obj
        )
        return Response({"slots": [slot.isoformat() for slot in slots]})
