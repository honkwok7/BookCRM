from django.conf import settings
from django.db import models

from core.models import BaseUUIDModel


class NotificationLog(BaseUUIDModel):
    class Channel(models.TextChoices):
        EMAIL = "email", "Email"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="notification_logs"
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notification_logs",
    )
    recipient_email = models.EmailField()
    notification_type = models.CharField(max_length=120)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.EMAIL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    related_booking = models.ForeignKey(
        "bookings.Booking",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notification_logs",
    )
    # Set for a waitlist email: the email is about ``related_booking``'s freed time, but it
    # belongs to this entry's customer (their timeline), not the booking's.
    related_waitlist_entry = models.ForeignKey(
        "bookings.WaitlistEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notification_logs",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)
    retry_count = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["organization", "notification_type", "status"])]
