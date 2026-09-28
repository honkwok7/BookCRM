from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from core.models import BaseUUIDModel

HEX_COLOR = RegexValidator(r"^#[0-9a-fA-F]{6}$", "Use a hex color such as #3b82f6.")


class Tag(BaseUUIDModel):
    """An organization-defined customer label ("VIP", "New patient", ...).

    Write through ``crm.services`` (create/update/delete/assign): slugs, duplicates and audit.
    """

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="tags"
    )
    name = models.CharField(max_length=60)
    slug = models.SlugField(max_length=80)
    color = models.CharField(max_length=7, blank=True, validators=[HEX_COLOR])

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="tag_unique_slug_per_org")
        ]

    def __str__(self) -> str:
        return self.name


class CustomerTag(BaseUUIDModel):
    """A tag on a customer. ``organization`` is stored so tenant scoping never needs a join."""

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="+"
    )
    customer = models.ForeignKey(
        "bookings.Customer", on_delete=models.CASCADE, related_name="customer_tags"
    )
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="customer_tags")
    tagged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["customer", "tag"], name="customer_tag_unique")
        ]
        indexes = [models.Index(fields=["organization", "tag"])]


class CustomerNote(BaseUUIDModel):
    """A note about a customer written by the team.

    ``internal`` notes need the ``customers.notes.private`` capability to read or write;
    ``customer_visible`` notes may be shown to the customer (portal, M5). Selectors enforce this.
    """

    class Visibility(models.TextChoices):
        INTERNAL = "internal", "Internal"
        CUSTOMER_VISIBLE = "customer_visible", "Visible to customer"

    class NoteType(models.TextChoices):
        GENERAL = "general", "General"
        CALL = "call", "Phone call"
        FOLLOW_UP = "follow_up", "Follow-up"
        ALERT = "alert", "Alert"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="+"
    )
    customer = models.ForeignKey(
        "bookings.Customer", on_delete=models.CASCADE, related_name="customer_notes"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    note_type = models.CharField(max_length=20, choices=NoteType.choices, default=NoteType.GENERAL)
    visibility = models.CharField(
        max_length=20, choices=Visibility.choices, default=Visibility.INTERNAL
    )
    content = models.TextField(max_length=10000)
    pinned = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-pinned", "-created_at")
        indexes = [models.Index(fields=["organization", "customer", "-created_at"])]


class CustomerActivity(BaseUUIDModel):
    """One entry in a customer's timeline. Written only by ``crm.activity.record_activity``.

    Holds ids and codes, never free text (no names, emails or note content), so the timeline
    survives anonymization without rewriting.
    """

    class Kind(models.TextChoices):
        CUSTOMER_CREATED = "customer_created", "Customer created"
        PROFILE_UPDATED = "profile_updated", "Profile updated"
        CONSENT_CHANGED = "consent_changed", "Consent changed"
        CUSTOMER_MERGED = "customer_merged", "Duplicate merged"
        APPOINTMENT_BOOKED = "appointment_booked", "Appointment booked"
        APPOINTMENT_RESCHEDULED = "appointment_rescheduled", "Appointment rescheduled"
        APPOINTMENT_CANCELLED = "appointment_cancelled", "Appointment cancelled"
        APPOINTMENT_COMPLETED = "appointment_completed", "Appointment completed"
        APPOINTMENT_NO_SHOW = "appointment_no_show", "No-show"
        NOTE_CREATED = "note_created", "Note added"
        TAG_ADDED = "tag_added", "Tag added"
        TAG_REMOVED = "tag_removed", "Tag removed"
        EMAIL_SENT = "email_sent", "Email sent"
        # Emitted by later milestones:
        SMS_SENT = "sms_sent", "SMS sent"
        FORM_COMPLETED = "form_completed", "Form completed"
        PAYMENT_RECORDED = "payment_recorded", "Payment recorded"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="+"
    )
    customer = models.ForeignKey(
        "bookings.Customer", on_delete=models.CASCADE, related_name="activities"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    kind = models.CharField(max_length=40, choices=Kind.choices)
    # What the entry is about (a booking, note, tag, ...): model label + primary key.
    subject_type = models.CharField(max_length=60, blank=True)
    subject_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    # Hidden from users without customers.notes.private (e.g. an internal note was added).
    internal = models.BooleanField(default=False)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-occurred_at", "-created_at")
        indexes = [models.Index(fields=["organization", "customer", "-occurred_at"])]
