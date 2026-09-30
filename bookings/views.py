import django_filters
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from bookings.models import Booking, WaitlistEntry
from bookings.selectors import bookings_visible_to
from bookings.serializers import (
    BookingCancelSerializer,
    BookingCreateSerializer,
    BookingCustomerSerializer,
    BookingRescheduleSerializer,
    BookingSerializer,
    BookingStatusHistorySerializer,
    BookingStatusSerializer,
    WaitlistEntrySerializer,
    is_team_request,
)
from bookings.services import change_booking_status, check_in, check_out, reschedule_booking
from bookings.waitlist import close_entry
from core.api import AuditedModelViewSetMixin
from core.audit import AuditAction
from core.filters import TenantModelChoiceFilter
from core.permissions import HasCapability
from organizations.models import OrganizationRole
from organizations.selectors import scope_queryset_by_organization
from organizations.tenancy import resolve_tenant
from services.models import Service
from staff.models import StaffProfile


class BookingFilter(django_filters.FilterSet):
    service = TenantModelChoiceFilter(Service)
    staff = TenantModelChoiceFilter(StaffProfile)

    class Meta:
        model = Booking
        fields = ("status", "service", "staff")


# Status changes other than cancelling are the team's (staff: their own appointments only,
# through the queryset).
TEAM_WRITE = [permissions.IsAuthenticated, HasCapability(write="appointments.manage")]


class BookingViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Appointments. There is no generic update/delete: every change goes through the
    booking service (cancel, reschedule, update_status) so the same rules apply everywhere."""

    permission_classes = [permissions.IsAuthenticated]
    filterset_class = BookingFilter
    ordering_fields = ("start_datetime", "created_at")

    def get_queryset(self):
        return bookings_visible_to(self.request)

    def get_serializer_class(self):
        tenant = resolve_tenant(self.request)
        if tenant is not None and tenant.role != OrganizationRole.CUSTOMER:
            return BookingSerializer
        return BookingCustomerSerializer

    def _respond(self, booking, status_code=status.HTTP_200_OK):
        return Response(self.get_serializer(booking).data, status=status_code)

    def _source(self, booking) -> str:
        """The channel of a change: the team's API, or a customer's own portal."""
        if is_team_request(self.request, booking.organization):
            return Booking.Source.API
        return Booking.Source.CUSTOMER_PORTAL

    def create(self, request, *args, **kwargs):
        serializer = BookingCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return self._respond(serializer.save(), status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        serializer = BookingCancelSerializer(
            data=request.data, context={"booking": self.get_object(), "request": request}
        )
        serializer.is_valid(raise_exception=True)
        return self._respond(serializer.save())

    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        booking = self.get_object()  # 404 before input validation
        serializer = BookingRescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_booking = reschedule_booking(
            booking=booking,
            new_start=serializer.validated_data["start_datetime"],
            actor=request.user,
            # A customer moving their own appointment: online-booking rules and deadline.
            public=not is_team_request(request, booking.organization),
            source=self._source(booking),
        )
        return self._respond(new_booking)

    @action(detail=True, methods=["post"], permission_classes=TEAM_WRITE)
    def update_status(self, request, pk=None):
        booking = self.get_object()  # 404 before input validation
        serializer = BookingStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = change_booking_status(
            booking=booking,
            new_status=serializer.validated_data["status"],
            note=serializer.validated_data["note"],
            reason=serializer.validated_data["reason"],
            actor=request.user,
            source=self._source(booking),
        )
        return self._respond(booking)

    @action(
        detail=True,
        methods=["get"],
        permission_classes=[permissions.IsAuthenticated, HasCapability(read="appointments.manage")],
    )
    def history(self, request, pk=None):
        """Every status change of the appointment, oldest first (team only)."""
        booking = self.get_object()
        rows = booking.status_history.select_related("changed_by").order_by("created_at")
        return Response(BookingStatusHistorySerializer(rows, many=True).data)

    @action(detail=True, methods=["post"], url_path="check-in", permission_classes=TEAM_WRITE)
    def check_in(self, request, pk=None):
        """The customer has arrived (confirmed -> checked in)."""
        booking = self.get_object()
        return self._respond(
            check_in(booking=booking, actor=request.user, source=self._source(booking))
        )

    @action(detail=True, methods=["post"], url_path="check-out", permission_classes=TEAM_WRITE)
    def check_out(self, request, pk=None):
        """The appointment is over (checked in or in progress -> completed)."""
        booking = self.get_object()
        return self._respond(
            check_out(booking=booking, actor=request.user, source=self._source(booking))
        )


class WaitlistViewSet(
    AuditedModelViewSetMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """The waitlist over the API. Entries are written only by ``bookings.waitlist``: create
    joins (or updates the customer's waiting entry), ``close/`` closes one. There is no
    generic update or delete, so the workflow fields (status, notification, expiry) can't be
    set by hand."""

    audit_actions = {"create": AuditAction.WAITLIST_ENTRY_CREATED}
    audited_by_service = ("create",)
    serializer_class = WaitlistEntrySerializer
    # Waitlist entries hold customer PII.
    permission_classes = [permissions.IsAuthenticated, HasCapability(read="waitlist.manage")]

    def get_queryset(self):
        queryset = WaitlistEntry.objects.select_related(
            "organization", "service", "preferred_staff"
        )
        return scope_queryset_by_organization(queryset, self.request)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        """Stop waiting: the entry no longer matches freed times."""
        entry = close_entry(entry=self.get_object(), actor=request.user)
        return Response(self.get_serializer(entry).data)
