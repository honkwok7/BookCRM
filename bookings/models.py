import random
import string
import uuid

from django.conf import settings
from django.db import models

from core.models import BaseUUIDModel


class Customer(BaseUUIDModel):
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
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    notes = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)
    total_bookings = models.PositiveIntegerField(default=0)
    no_show_count = models.PositiveIntegerField(default=0)
    last_appointment = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("organization", "email")
        indexes = [models.Index(fields=["organization", "email"])]


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
