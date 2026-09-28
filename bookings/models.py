import random
import string
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from core.models import BaseUUIDModel


class Customer(BaseUUIDModel):
    """The CRM customer of one organization (shown as "Client" or "Patient" in the UI later).

    Write through ``crm.services`` (create, update, merge, anonymize): it enforces duplicates,
    consent stamping and auditing. ``name`` is kept in sync with first/last name for callers
    that still read it.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"
        ARCHIVED = "archived", "Archived"
        ANONYMIZED = "anonymized", "Anonymized"

    class ContactMethod(models.TextChoices):
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"
        PHONE = "phone", "Phone call"
        NONE = "none", "Do not contact"

    class Source(models.TextChoices):
        PUBLIC_BOOKING = "public_booking", "Online booking"
        RECEPTION = "reception", "Reception"
        STAFF = "staff", "Staff"
        REFERRAL = "referral", "Referral"
        WALK_IN = "walk_in", "Walk-in"
        IMPORT = "import", "Import"
        OTHER = "other", "Other"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="customers"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_profiles",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    # Identity
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    preferred_name = models.CharField(max_length=150, blank=True)
    name = models.CharField(max_length=255)  # "First Last", kept in sync on save
    birthday = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=50, blank=True)  # optional, free text
    pronouns = models.CharField(max_length=50, blank=True)

    # Contact: an email or a phone number is required (phone-only customers are allowed).
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    secondary_phone = models.CharField(max_length=30, blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, blank=True)
    region = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, blank=True)  # ISO 3166-1 alpha-2

    # Preferences
    preferred_language = models.CharField(max_length=10, blank=True)
    preferred_contact_method = models.CharField(
        max_length=10, choices=ContactMethod.choices, blank=True
    )
    assigned_staff = models.ForeignKey(
        "staff.StaffProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_customers",
    )
    preferred_staff = models.ForeignKey(
        "staff.StaffProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferring_customers",
    )

    # Consent (changes are stamped and audited by crm.services)
    marketing_consent = models.BooleanField(default=False)
    email_consent = models.BooleanField(default=False)
    sms_consent = models.BooleanField(default=False)
    consent_updated_at = models.DateTimeField(null=True, blank=True)

    # Internal
    source = models.CharField(max_length=20, choices=Source.choices, blank=True)
    alerts = models.CharField(max_length=255, blank=True)  # short internal alert text
    notes = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)  # replaced by crm.Tag in M2.2
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_customers",
    )
    anonymized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "organization",
                Lower("email"),
                condition=~Q(email=""),
                name="customer_unique_email_per_org",
            ),
            models.CheckConstraint(
                condition=~Q(email="") | ~Q(phone="") | Q(status="anonymized"),
                name="customer_email_or_phone_required",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "email"]),
            models.Index(fields=["organization", "last_name", "first_name"]),
            models.Index(fields=["organization", "phone"]),
            models.Index(fields=["organization", "status"]),
        ]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        return self.preferred_name or self.name

    @staticmethod
    def split_name(name: str) -> tuple[str, str]:
        """Split a full name: the last whitespace-separated token is the last name."""
        parts = name.split()
        if len(parts) < 2:
            return (parts[0] if parts else ""), ""
        return " ".join(parts[:-1]), parts[-1]

    def sync_name(self) -> None:
        if not (self.first_name or self.last_name) and self.name:
            self.first_name, self.last_name = self.split_name(self.name)
        self.name = " ".join(part for part in (self.first_name, self.last_name) if part)

    def save(self, *args, **kwargs):
        self.sync_name()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and {"name", "first_name", "last_name"} & set(update_fields):
            kwargs["update_fields"] = {*update_fields, "name", "first_name", "last_name"}
        super().save(*args, **kwargs)


class Booking(BaseUUIDModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        CHECKED_IN = "checked_in", "Checked In"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No Show"
        REJECTED = "rejected", "Rejected"

    class PaymentStatus(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PAID = "paid", "Paid"
        REFUNDED = "refunded", "Refunded"

    public_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    reference = models.CharField(max_length=20, unique=True, db_index=True)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="bookings"
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="bookings"
    )
    customer_name = models.CharField(max_length=255)
    customer_email = models.EmailField()
    customer_phone = models.CharField(max_length=30, blank=True)
    service = models.ForeignKey(
        "services.Service", on_delete=models.PROTECT, related_name="bookings"
    )
    staff = models.ForeignKey(
        "staff.StaffProfile", on_delete=models.PROTECT, related_name="bookings"
    )
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    customer_timezone = models.CharField(max_length=64, default="UTC")
    organization_timezone = models.CharField(max_length=64, default="UTC")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    payment_status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID
    )
    price_snapshot = models.DecimalField(max_digits=12, decimal_places=2)
    duration_snapshot_minutes = models.PositiveIntegerField()
    customer_notes = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)
    cancellation_reason = models.TextField(blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_bookings",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    rescheduled_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="reschedules"
    )

    class Meta:
        indexes = [
            models.Index(fields=["organization", "start_datetime", "status"]),
            models.Index(fields=["organization", "customer_email"]),
            models.Index(fields=["organization", "staff", "start_datetime"]),
        ]

    @staticmethod
    def generate_reference(year: int | None = None) -> str:
        suffix = "".join(random.choices(string.digits, k=6))
        year = year or 2026
        return f"SCH-{year}-{suffix}"

    def __str__(self) -> str:
        return self.reference


class BookingStatusHistory(BaseUUIDModel):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="status_history")
    old_status = models.CharField(max_length=20, blank=True)
    new_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    note = models.TextField(blank=True)


class BookingActivityLog(BaseUUIDModel):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="activity_logs")
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="booking_activity_logs"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=120)
    metadata = models.JSONField(default=dict, blank=True)


class WaitlistEntry(BaseUUIDModel):
    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting"
        NOTIFIED = "notified", "Notified"
        CLOSED = "closed", "Closed"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="waitlist_entries"
    )
    service = models.ForeignKey(
        "services.Service", on_delete=models.CASCADE, related_name="waitlist_entries"
    )
    preferred_staff = models.ForeignKey(
        "staff.StaffProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="waitlist_entries",
    )
    preferred_start_date = models.DateField(null=True, blank=True)
    preferred_end_date = models.DateField(null=True, blank=True)
    customer_name = models.CharField(max_length=255)
    customer_email = models.EmailField()
    customer_phone = models.CharField(max_length=30, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.WAITING)
