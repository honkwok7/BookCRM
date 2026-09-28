from rest_framework import serializers

from bookings.models import Booking, Customer, WaitlistEntry
from bookings.services import cancel_booking, create_booking
from core.api import TenantScopedModelSerializer
from organizations.permissions import Capability
from organizations.selectors import get_request_organization
from organizations.tenancy import (
    get_public_organization,
    requested_organization_slug,
    resolve_tenant,
)
from services.models import Service
from staff.models import StaffProfile


class CustomerSerializer(TenantScopedModelSerializer):
    class Meta:
        model = Customer
        fields = (
            "id",
            "organization",
            "user",
            "name",
            "email",
            "phone",
            "notes",
            "tags",
            "total_bookings",
            "no_show_count",
            "last_appointment",
            "created_at",
            "updated_at",
        )
        # Linking a customer record to a user account is not an API operation.
        read_only_fields = (
            "id",
            "organization",
            "user",
            "total_bookings",
            "no_show_count",
            "last_appointment",
            "created_at",
            "updated_at",
        )

    def validate_email(self, email):
        organization = get_request_organization(self.context["request"])
        duplicates = Customer.objects.filter(organization=organization, email__iexact=email)
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise serializers.ValidationError("A customer with this email already exists.")
        return email


BOOKING_CUSTOMER_FIELDS = (
    "id",
    "public_uuid",
    "reference",
    "organization",
    "service",
    "staff",
    "start_datetime",
    "end_datetime",
    "customer_name",
    "customer_email",
    "customer_phone",
    "customer_timezone",
    "organization_timezone",
    "status",
    "payment_status",
    "price_snapshot",
    "duration_snapshot_minutes",
    "customer_notes",
    "cancellation_reason",
    "cancelled_at",
    "rescheduled_from",
    "created_at",
    "updated_at",
)


class BookingCustomerSerializer(serializers.ModelSerializer):
    """What a customer may see about their own appointment (no internal notes)."""

    class Meta:
        model = Booking
        fields = BOOKING_CUSTOMER_FIELDS
        read_only_fields = fields


class BookingSerializer(serializers.ModelSerializer):
    """Team view. Appointments change only through the booking service actions."""

    class Meta:
        model = Booking
        fields = (*BOOKING_CUSTOMER_FIELDS, "customer", "internal_notes", "cancelled_by")
        read_only_fields = fields


class BookingStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Booking.Status.choices)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class BookingRescheduleSerializer(serializers.Serializer):
    start_datetime = serializers.DateTimeField()


class BookingCreateSerializer(serializers.Serializer):
    service = serializers.UUIDField()
    staff = serializers.UUIDField()
    start_datetime = serializers.DateTimeField()
    customer_name = serializers.CharField(max_length=255)
    customer_email = serializers.EmailField()
    customer_phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    customer_timezone = serializers.CharField(max_length=64, required=False, default="UTC")
    customer_notes = serializers.CharField(required=False, allow_blank=True)

    def create(self, validated_data):
        request = self.context["request"]
        tenant = resolve_tenant(request)
        if tenant is not None and tenant.has(Capability.APPOINTMENTS_MANAGE):
            # Team member booking on behalf of a customer in their own organization.
            organization = tenant.organization
            customer_user = None
        else:
            # Self-service booking: only organizations with a public booking page.
            slug = requested_organization_slug(request) or (
                tenant.organization.slug if tenant else None
            )
            organization = get_public_organization(slug)
            customer_user = request.user
        if organization is None:
            raise serializers.ValidationError({"organization": "Organization not found."})

        service = Service.objects.filter(
            id=validated_data["service"],
            organization=organization,
            is_active=True,
            is_archived=False,
        ).first()
        staff_profile = StaffProfile.objects.filter(
            id=validated_data["staff"],
            organization=organization,
            is_active=True,
            is_accepting_bookings=True,
        ).first()
        if service is None or staff_profile is None:
            raise serializers.ValidationError({"detail": "Service or staff not found."})

        return create_booking(
            organization=organization,
            service=service,
            staff_profile=staff_profile,
            customer_name=validated_data["customer_name"],
            customer_email=validated_data["customer_email"],
            customer_phone=validated_data.get("customer_phone", ""),
            start_datetime=validated_data["start_datetime"],
            customer_timezone=validated_data.get("customer_timezone", "UTC"),
            customer_notes=validated_data.get("customer_notes", ""),
            actor=request.user,
            customer_user=customer_user,
        )


class BookingCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)

    def save(self, **kwargs):
        booking = self.context["booking"]
        actor = (
            self.context["request"].user if self.context["request"].user.is_authenticated else None
        )
        return cancel_booking(
            booking=booking, actor=actor, reason=self.validated_data.get("reason", "")
        )


class WaitlistEntrySerializer(TenantScopedModelSerializer):
    class Meta:
        model = WaitlistEntry
        fields = (
            "id",
            "organization",
            "service",
            "preferred_staff",
            "preferred_start_date",
            "preferred_end_date",
            "customer_name",
            "customer_email",
            "customer_phone",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "created_at", "updated_at")
