from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from bookings.models import Customer, WaitlistEntry
from bookings.selectors import bookings_visible_to
from bookings.serializers import (
    BookingCancelSerializer,
    BookingCreateSerializer,
    BookingCustomerSerializer,
    BookingRescheduleSerializer,
    BookingSerializer,
    BookingStatusSerializer,
    CustomerSerializer,
    WaitlistEntrySerializer,
)
from bookings.services import change_booking_status, reschedule_booking
from core.api import AuditedModelViewSetMixin
from core.audit import AuditAction
from core.permissions import HasCapability
from organizations.models import OrganizationRole
from organizations.selectors import scope_queryset_by_organization
from organizations.tenancy import resolve_tenant


class BookingViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Appointments. There is no generic update/delete: every change goes through the
    booking service (cancel, reschedule, update_status) so the same rules apply everywhere."""

    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ("status", "service", "staff")
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
        serializer = BookingRescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_booking = reschedule_booking(
            booking=self.get_object(),
            new_start=serializer.validated_data["start_datetime"],
            actor=request.user,
        )
        return self._respond(new_booking)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[
            permissions.IsAuthenticated,
            HasCapability(write="appointments.manage"),
        ],
    )
    def update_status(self, request, pk=None):
        serializer = BookingStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = change_booking_status(
            booking=self.get_object(),
            new_status=serializer.validated_data["status"],
            note=serializer.validated_data["note"],
            actor=request.user,
        )
        return self._respond(booking)


class CustomerViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    audit_actions = {
        "create": AuditAction.CUSTOMER_CREATED,
        "update": AuditAction.CUSTOMER_UPDATED,
        "delete": AuditAction.CUSTOMER_DELETED,
    }
    audit_redact_fields = (
        "name",
        "email",
        "phone",
        "notes",
        "tags",
    )
    serializer_class = CustomerSerializer
    permission_classes = [
        permissions.IsAuthenticated,
        HasCapability(read="customers.view", write="customers.manage"),
    ]
    search_fields = ("name", "email", "phone")

    def get_queryset(self):
        queryset = Customer.objects.select_related("organization", "user")
        return scope_queryset_by_organization(queryset, self.request)


class WaitlistViewSet(AuditedModelViewSetMixin, viewsets.ModelViewSet):
    audit_actions = {
        "create": AuditAction.WAITLIST_ENTRY_CREATED,
        "update": AuditAction.WAITLIST_ENTRY_UPDATED,
        "delete": AuditAction.WAITLIST_ENTRY_DELETED,
    }
    audit_redact_fields = (
        "customer_name",
        "customer_email",
        "customer_phone",
    )
    serializer_class = WaitlistEntrySerializer
    # Waitlist entries hold customer PII.
    # Public/portal joining arrives with the waitlist service (M4.7).
    permission_classes = [permissions.IsAuthenticated, HasCapability(read="waitlist.manage")]

    def get_queryset(self):
        queryset = WaitlistEntry.objects.select_related(
            "organization", "service", "preferred_staff"
        )
        return scope_queryset_by_organization(queryset, self.request)
