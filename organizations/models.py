import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.template.defaultfilters import slugify
from django.utils import timezone

from core.models import BaseUUIDModel


class Organization(BaseUUIDModel):
    public_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    logo = models.ImageField(upload_to="organizations/logos/", blank=True, null=True)
    description = models.TextField(blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    website = models.URLField(blank=True)
    address = models.TextField(blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    currency = models.CharField(max_length=10, default="USD")
    booking_page_enabled = models.BooleanField(default=True)
    booking_page_theme = models.JSONField(default=dict, blank=True)
    default_appointment_rules = models.JSONField(default=dict, blank=True)
    allow_guest_booking = models.BooleanField(default=True)
    reminder_hours_before = models.PositiveIntegerField(default=24)
    second_reminder_hours_before = models.PositiveIntegerField(default=2)
    is_active = models.BooleanField(default=True)
    is_suspended = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class OrganizationRole(models.TextChoices):
    OWNER = "owner", "Owner"
    MANAGER = "manager", "Manager"
    STAFF = "staff", "Staff"
    CUSTOMER = "customer", "Customer"


class OrganizationMembership(BaseUUIDModel):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    role = models.CharField(max_length=20, choices=OrganizationRole.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("organization", "user")
        indexes = [models.Index(fields=["organization", "role", "is_active"])]

    def __str__(self) -> str:
        return f"{self.user} @ {self.organization} ({self.role})"


class OrganizationInvitation(BaseUUIDModel):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=OrganizationRole.choices)
    token = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    inviter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_org_invitations",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_org_invitations",
    )

    class Meta:
        indexes = [models.Index(fields=["organization", "email", "expires_at"])]

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(48)

    @classmethod
    def default_expiry(cls):
        return timezone.now() + timedelta(days=7)

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_usable(self) -> bool:
        return self.accepted_at is None and not self.is_expired

    def __str__(self) -> str:
        return f"Invite {self.email} to {self.organization}"
