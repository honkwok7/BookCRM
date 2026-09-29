from datetime import datetime, timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from bookings.selectors import bookable_services, bookable_staff
from core.api import AuditedModelViewSetMixin
from core.audit import AuditAction
from core.permissions import HasCapability
from locations.models import Location
from organizations.selectors import scope_queryset_by_organization
from organizations.tenancy import get_public_organization, requested_organization_slug
from scheduling.availability import AvailabilityService
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
from services.selectors import service_offered_at

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
    MAX_DAYS = 31

    @action(detail=False, methods=["get"], url_path="available-slots")
    def available_slots(self, request):
        """Free times for a service on the public booking page (no sign-in).

        ``?organization=<slug>&service=<id>&date=YYYY-MM-DD`` plus optional ``staff`` (else any
        provider), ``location`` (else the default location) and ``end_date`` (at most 31 days).
        ``slots`` lists the start times in the location's time zone; ``availability`` adds who is
        free at each. Only services bookable online and staff visible online are considered.
        """
        # Public endpoint: the organization comes from the public booking slug, never a membership.
        org = get_public_organization(requested_organization_slug(request))
        if not org:
            return Response({"detail": "Organization not found"}, status=404)

        params = request.query_params
        service_id, staff_id, location_id = (
            params.get("service"),
            params.get("staff"),
            params.get("location"),
        )
        date_value = params.get("date")
        if not (service_id and date_value):
            return Response({"detail": "service and date are required"}, status=400)

        try:
            start_date = datetime.strptime(date_value, "%Y-%m-%d").date()
            end_date = datetime.strptime(params.get("end_date") or date_value, "%Y-%m-%d").date()
            service = bookable_services(org, public=True).filter(id=service_id).first()
            staff_profile = None
            if staff_id:
                staff_profile = bookable_staff(org, public=True).filter(id=staff_id).first()
            location = None
            if location_id:
                location = Location.objects.filter(
                    id=location_id, organization=org, is_active=True, booking_enabled=True
                ).first()
        except ValueError, DjangoValidationError:
            return Response({"detail": "Invalid service, staff, location or date"}, status=400)
        if not start_date <= end_date <= start_date + timedelta(days=self.MAX_DAYS - 1):
            return Response(
                {"detail": f"end_date must be within {self.MAX_DAYS} days of date"}, status=400
            )
        if (
            service is None
            or (staff_id and staff_profile is None)
            or (location_id and location is None)
        ):
            return Response({"detail": "Service, staff or location not found"}, status=404)

        engine = AvailabilityService(org, service, location=location, public=True)
        if not service_offered_at(service, engine.location):
            return Response({"detail": "Service, staff or location not found"}, status=404)
        if staff_profile is not None and not engine.providers(staff_profile):
            # A provider who doesn't offer the service here answers like a missing one.
            return Response({"detail": "Service, staff or location not found"}, status=404)
        slots = engine.get_available_slots(start_date, end_date, staff=staff_profile)
        zone = engine.zone
        return Response(
            {
                "timezone": engine.location.timezone,
                "location": str(engine.location.pk),
                "slots": [slot.start.astimezone(zone).isoformat() for slot in slots],
                "availability": [
                    {
                        "start": slot.start.astimezone(zone).isoformat(),
                        "staff": [
                            {
                                "id": str(candidate.staff.pk),
                                "name": candidate.staff.public_name,
                                "end": candidate.end.astimezone(zone).isoformat(),
                            }
                            for candidate in slot.candidates
                        ],
                    }
                    for slot in slots
                ],
            }
        )
