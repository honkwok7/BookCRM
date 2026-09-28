from zoneinfo import available_timezones

from django.db import transaction
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from bookings.models import Booking, Customer, WaitlistEntry
from bookings.selectors import bookable_services, bookable_staff
from bookings.services import cancel_booking, create_booking
from core.api import TenantPrimaryKeyRelatedField, TenantScopedModelSerializer
from crm.models import Tag
from crm.selectors import customer_stats
from crm.serializers import TagSummarySerializer
from crm.services import create_customer, set_customer_tags, update_customer
from organizations.permissions import Capability
from organizations.selectors import get_request_organization
from organizations.tenancy import (
    get_public_organization,
    requested_organization_slug,
    resolve_tenant,
)


class CustomerSerializer(TenantScopedModelSerializer):
    """Team view of a CRM customer. Writes go through ``crm.services`` (checks + audit)."""

    display_name = serializers.CharField(read_only=True)
    tags = TagSummarySerializer(source="tag_set", many=True, read_only=True)
    # Setting tag_ids replaces the customer's tags (each change audited); tenant-scoped ids.
    tag_ids = TenantPrimaryKeyRelatedField(
        many=True, queryset=Tag.objects.all(), write_only=True, required=False
    )

    class Meta:
        model = Customer
        fields = (
            "id",
            "organization",
            "user",
            "status",
            "display_name",
            "name",
            "first_name",
            "last_name",
            "preferred_name",
            "birthday",
            "gender",
            "pronouns",
            "email",
            "phone",
            "secondary_phone",
            "address_line1",
            "address_line2",
            "city",
            "region",
            "postal_code",
            "country",
            "preferred_language",
            "preferred_contact_method",
            "assigned_staff",
            "preferred_staff",
            "marketing_consent",
            "email_consent",
            "sms_consent",
            "consent_updated_at",
            "source",
            "alerts",
            "notes",
            "tags",
            "tag_ids",
            "created_by",
            "anonymized_at",
            "created_at",
            "updated_at",
        )
        # Linking a customer record to a user account is not an API operation.
        read_only_fields = (
            "id",
            "organization",
            "user",
            "consent_updated_at",
            "created_by",
            "anonymized_at",
            "created_at",
            "updated_at",
        )
        # ``name`` may be sent instead of first/last name; it is always returned as "First Last".
        extra_kwargs = {"name": {"required": False}}

    def validate_email(self, email):
        organization = get_request_organization(self.context["request"])
        duplicates = Customer.objects.filter(organization=organization, email__iexact=email)
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if email and duplicates.exists():
            raise serializers.ValidationError("A customer with this email already exists.")
        return email

    def validate_status(self, value):
        if value == Customer.Status.ANONYMIZED:
            raise serializers.ValidationError("Customers are anonymized with a separate action.")
        return value

    @staticmethod
    def _split_full_name(validated_data):
        """``name`` is accepted as a shortcut: split into first/last unless those are given."""
        name = validated_data.pop("name", None)
        if name and not ({"first_name", "last_name"} & set(validated_data)):
            validated_data["first_name"], validated_data["last_name"] = Customer.split_name(name)
        return validated_data

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        tags = validated_data.pop("tag_ids", None)
        customer = create_customer(
            organization=get_request_organization(request),
            actor=request.user,
            **self._split_full_name(validated_data),
        )
        if tags:
            set_customer_tags(customer=customer, tags=tags, actor=request.user)
        return customer

    @transaction.atomic
    def update(self, instance, validated_data):
        actor = self.context["request"].user
        tags = validated_data.pop("tag_ids", None)
        customer = instance
        if validated_data:
            customer = update_customer(
                customer=instance, actor=actor, **self._split_full_name(validated_data)
            )
        if tags is not None:
            set_customer_tags(customer=customer, tags=tags, actor=actor)
        return customer


class CustomerDetailSerializer(CustomerSerializer):
    stats = serializers.SerializerMethodField()

    class Meta(CustomerSerializer.Meta):
        fields = (*CustomerSerializer.Meta.fields, "stats")

    @extend_schema_field(serializers.DictField())
    def get_stats(self, customer):
        return customer_stats(customer)


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

    def validate_customer_timezone(self, value):
        if value not in available_timezones():
            raise serializers.ValidationError("Unknown time zone.")
        return value

    def create(self, validated_data):
        request = self.context["request"]
        tenant = resolve_tenant(request)
        is_team = tenant is not None and tenant.has(Capability.APPOINTMENTS_MANAGE)
        if is_team:
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

        # Self-service bookings may only use services published on the booking page.
        service = (
            bookable_services(organization, public=not is_team)
            .filter(id=validated_data["service"])
            .first()
        )
        staff_profile = bookable_staff(organization).filter(id=validated_data["staff"]).first()
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
