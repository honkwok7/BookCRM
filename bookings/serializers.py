from zoneinfo import available_timezones

from django.db import transaction
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from bookings.models import Booking, BookingStatusHistory, Customer, WaitlistEntry
from bookings.selectors import bookable_services, bookable_staff
from bookings.services import cancel_booking, create_booking
from core.api import TenantPrimaryKeyRelatedField, TenantScopedModelSerializer
from crm.models import Tag
from crm.selectors import customer_stats
from crm.serializers import TagSummarySerializer
from crm.services import create_customer, set_customer_tags, update_customer
from locations.models import Location
from organizations.models import OrganizationRole
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
    last_visit = serializers.SerializerMethodField()
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
            "last_visit",
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

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_last_visit(self, customer):
        # Annotated by the customer list (latest completed appointment); None elsewhere.
        value = getattr(customer, "last_visit", None)
        return serializers.DateTimeField().to_representation(value) if value else None

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
    "location",
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
    "checked_in_at",
    "completed_at",
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
        fields = (
            *BOOKING_CUSTOMER_FIELDS,
            "customer",
            "source",
            "created_by",
            "buffer_before_minutes",
            "buffer_after_minutes",
            "internal_notes",
            "cancelled_by",
        )
        read_only_fields = fields


class BookingStatusHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingStatusHistory
        fields = (
            "id",
            "old_status",
            "new_status",
            "reason",
            "source",
            "note",
            "changed_by",
            "created_at",
        )
        read_only_fields = fields


class BookingStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Booking.Status.choices)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    note = serializers.CharField(required=False, allow_blank=True, default="")


class BookingRescheduleSerializer(serializers.Serializer):
    start_datetime = serializers.DateTimeField()


def is_team_request(request, organization=None) -> bool:
    """The user acts as a team member of the booking's organization (not as its customer)."""
    tenant = resolve_tenant(request)
    if tenant is None or tenant.role == OrganizationRole.CUSTOMER:
        return False
    return organization is None or tenant.organization.pk == organization.pk


class BookingCreateSerializer(serializers.Serializer):
    service = serializers.UUIDField()
    staff = serializers.UUIDField()
    location = serializers.UUIDField(required=False, allow_null=True)
    start_datetime = serializers.DateTimeField()
    customer_name = serializers.CharField(max_length=255)
    customer_email = serializers.EmailField()
    customer_phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    customer_timezone = serializers.CharField(max_length=64, required=False, default="UTC")
    customer_notes = serializers.CharField(required=False, allow_blank=True)
    # Also accepted as the ``Idempotency-Key`` header: a retry with the same key returns the
    # booking made the first time instead of a second one.
    idempotency_key = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )

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
        staff_profile = (
            bookable_staff(organization, public=not is_team)
            .filter(id=validated_data["staff"])
            .first()
        )
        if service is None or staff_profile is None:
            raise serializers.ValidationError({"detail": "Service or staff not found."})
        location = None
        if validated_data.get("location"):
            locations = Location.objects.filter(organization=organization, is_active=True)
            if not is_team:
                locations = locations.filter(booking_enabled=True)
            location = locations.filter(id=validated_data["location"]).first()
            if location is None:
                raise serializers.ValidationError({"location": "Location not found."})

        # The booking service checks the rest for every caller: the provider offers the
        # service there, the time is free (and, for customers, bookable online).
        return create_booking(
            organization=organization,
            service=service,
            staff_profile=staff_profile,
            location=location,
            source=Booking.Source.API if is_team else Booking.Source.CUSTOMER_PORTAL,
            public=not is_team,
            customer_name=validated_data["customer_name"],
            customer_email=validated_data["customer_email"],
            customer_phone=validated_data.get("customer_phone", ""),
            start_datetime=validated_data["start_datetime"],
            customer_timezone=validated_data.get("customer_timezone", "UTC"),
            customer_notes=validated_data.get("customer_notes", ""),
            actor=request.user,
            customer_user=customer_user,
            idempotency_key=(
                validated_data.get("idempotency_key") or request.headers.get("Idempotency-Key", "")
            )[:64],
        )


class BookingCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)

    def save(self, **kwargs):
        booking = self.context["booking"]
        request = self.context["request"]
        actor = request.user if request.user.is_authenticated else None
        team = is_team_request(request, booking.organization)
        return cancel_booking(
            booking=booking,
            actor=actor,
            reason=self.validated_data.get("reason", ""),
            # A customer cancelling their own appointment is held to the deadline.
            enforce_deadline=not team,
            source=Booking.Source.API if team else Booking.Source.CUSTOMER_PORTAL,
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
            "customer",
            "location",
            "time_of_day",
            "source",
            "notified_at",
            "expires_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "organization",
            "customer",
            "source",
            "notified_at",
            "created_at",
            "updated_at",
        )
