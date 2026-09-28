from datetime import datetime

from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.permissions import IsOrganizationManagerOrOwner
from organizations.selectors import get_request_organization, scope_queryset_by_organization
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


class WeeklyAvailabilityViewSet(viewsets.ModelViewSet):
    serializer_class = WeeklyAvailabilitySerializer
    permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]

    def get_queryset(self):
        queryset = WeeklyAvailability.objects.select_related("organization", "staff")
        return scope_queryset_by_organization(queryset, self.request)


class AvailabilityExceptionViewSet(viewsets.ModelViewSet):
    serializer_class = AvailabilityExceptionSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]

    def get_queryset(self):
        queryset = AvailabilityException.objects.select_related("organization", "staff")
        return scope_queryset_by_organization(queryset, self.request)


class TimeOffViewSet(viewsets.ModelViewSet):
    serializer_class = TimeOffSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]

    def get_queryset(self):
        queryset = TimeOff.objects.select_related("organization", "staff")
        return scope_queryset_by_organization(queryset, self.request)


class OrganizationHolidayViewSet(viewsets.ModelViewSet):
    serializer_class = OrganizationHolidaySerializer
    permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]

    def get_queryset(self):
        queryset = OrganizationHoliday.objects.select_related("organization")
        return scope_queryset_by_organization(queryset, self.request)


class SlotViewSet(viewsets.ViewSet):
    permission_classes = [permissions.AllowAny]

    @action(detail=False, methods=["get"], url_path="available-slots")
    def available_slots(self, request):
        org = get_request_organization(request)
        if not org:
            return Response({"detail": "Organization not found"}, status=400)

        service_id = request.query_params.get("service")
        staff_id = request.query_params.get("staff")
        date_value = request.query_params.get("date")
        if not (service_id and staff_id and date_value):
            return Response({"detail": "service, staff and date are required"}, status=400)

        service = Service.objects.get(id=service_id, organization=org)
        staff_profile = StaffProfile.objects.get(
            id=staff_id, organization=org, is_active=True, is_accepting_bookings=True
        )
        date_obj = datetime.strptime(date_value, "%Y-%m-%d").date()

        slots = generate_slots(
            organization=org, service=service, staff_profile=staff_profile, date=date_obj
        )
        return Response({"slots": [slot.isoformat() for slot in slots]})
