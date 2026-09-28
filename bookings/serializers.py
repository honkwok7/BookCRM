from rest_framework import serializers

from bookings.models import Booking, Customer, WaitlistEntry
from bookings.services import cancel_booking, create_booking
from organizations.selectors import get_request_organization
from services.models import Service
from staff.models import StaffProfile


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = "__all__"
        read_only_fields = (
            "id",
            "organization",
            "total_bookings",
            "no_show_count",
            "last_appointment",
            "created_at",
            "updated_at",
        )


class BookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = "__all__"
        read_only_fields = (
            "id",
            "public_uuid",
            "reference",
            "organization",
            "customer",
            "price_snapshot",
            "duration_snapshot_minutes",
            "cancelled_by",
            "cancelled_at",
            "created_at",
            "updated_at",
        )


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
        organization = get_request_organization(request)
        service = Service.objects.get(
            id=validated_data["service"],
            organization=organization,
            is_active=True,
            is_archived=False,
        )
        staff_profile = StaffProfile.objects.get(
            id=validated_data["staff"],
            organization=organization,
            is_active=True,
            is_accepting_bookings=True,
        )

        booking = create_booking(
            organization=organization,
            service=service,
            staff_profile=staff_profile,
            customer_name=validated_data["customer_name"],
            customer_email=validated_data["customer_email"],
            customer_phone=validated_data.get("customer_phone", ""),
            start_datetime=validated_data["start_datetime"],
            customer_timezone=validated_data.get("customer_timezone", "UTC"),
            customer_notes=validated_data.get("customer_notes", ""),
            actor=request.user if request.user.is_authenticated else None,
            customer_user=request.user if request.user.is_authenticated else None,
        )
        return booking


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


class WaitlistEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = WaitlistEntry
        fields = "__all__"
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def create(self, validated_data):
        validated_data["organization"] = get_request_organization(self.context["request"])
        return super().create(validated_data)
