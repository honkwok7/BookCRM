import django_filters
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.api import AuditedModelViewSetMixin
from core.filters import TenantModelChoiceFilter
from core.permissions import HasCapability
from locations.models import Location, LocationClosure
from locations.selectors import closures_for, locations_for
from locations.serializers import (
    LocationClosureSerializer,
    LocationHoursReplaceSerializer,
    LocationHoursSerializer,
    LocationSerializer,
)
from locations.services import (
    delete_closure,
    delete_location,
    set_default_location,
    set_location_hours,
)
from organizations.permissions import Capability
from organizations.selectors import get_request_organization

LOCATION_ACCESS = HasCapability(read=Capability.LOCATIONS_VIEW, write=Capability.LOCATIONS_MANAGE)


class LocationViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Locations of the organization, with their weekly opening hours.

    Read: ``locations.view``. Write: ``locations.manage``. Creating or reactivating a location
    counts against the plan's location limit (409 ``plan_limit``). The default location can't
    be deactivated or deleted (409 ``default_location``); move the default first with
    ``make-default``.
    """

    audited_by_service = ("create", "update", "delete")
    serializer_class = LocationSerializer
    permission_classes = [permissions.IsAuthenticated, LOCATION_ACCESS]
    filterset_fields = ("is_active", "booking_enabled", "is_default")
    search_fields = ("name", "city", "address_line1")
    ordering_fields = ("name", "created_at")
    ordering = ("-is_default", "name")

    def get_queryset(self):
        organization = get_request_organization(self.request)
        if organization is None:
            return Location.objects.none()
        return locations_for(organization).select_related("organization")

    def get_serializer_class(self):
        if self.action == "hours":
            if self.request.method == "GET":
                return LocationHoursSerializer
            return LocationHoursReplaceSerializer
        return LocationSerializer

    def perform_destroy(self, instance):
        delete_location(location=instance, actor=self.request.user)

    @extend_schema(
        request=LocationHoursReplaceSerializer, responses=LocationHoursSerializer(many=True)
    )
    @action(detail=True, methods=["get", "put"])
    def hours(self, request, pk=None):
        """Weekly opening hours, Monday = 0. ``PUT {"hours": [...]}`` replaces them all; at most
        two non-overlapping periods a day. No hours at all means the location sets no limit."""
        location = self.get_object()
        if request.method == "PUT":
            serializer = LocationHoursReplaceSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            set_location_hours(location=location, periods=serializer.periods(), actor=request.user)
        rows = location.hours.order_by("weekday", "opens_at")
        return Response(LocationHoursSerializer(rows, many=True).data)

    @extend_schema(request=None, responses=LocationSerializer)
    @action(detail=True, methods=["post"], url_path="make-default")
    def make_default(self, request, pk=None):
        """Make this (active) location the organization's default."""
        location = set_default_location(location=self.get_object(), actor=request.user)
        location = self.get_queryset().get(pk=location.pk)
        return Response(LocationSerializer(location, context={"request": request}).data)


class LocationClosureFilter(django_filters.FilterSet):
    location = TenantModelChoiceFilter(Location)
    ends_after = django_filters.DateFilter(field_name="end_date", lookup_expr="gte")
    starts_before = django_filters.DateFilter(field_name="start_date", lookup_expr="lte")

    class Meta:
        model = LocationClosure
        fields = ("location", "all_day")


class LocationClosureViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    """Dates a location is closed, all day or between two times each day.

    ``?location=<id>&ends_after=<date>`` lists the upcoming closures of one location.
    """

    audited_by_service = ("create", "update", "delete")
    serializer_class = LocationClosureSerializer
    permission_classes = [permissions.IsAuthenticated, LOCATION_ACCESS]
    filterset_class = LocationClosureFilter
    ordering_fields = ("start_date", "created_at")
    ordering = ("start_date", "start_time")

    def get_queryset(self):
        organization = get_request_organization(self.request)
        if organization is None:
            return LocationClosure.objects.none()
        return closures_for(organization)

    def perform_destroy(self, instance):
        delete_closure(closure=instance, actor=self.request.user)
