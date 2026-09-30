"""Platform-level records (M5.5b): announcements, feature flags and impersonation sessions.
None of them belongs to an organization's data; writes go through ``saas.services``."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import BaseUUIDModel


class Announcement(BaseUUIDModel):
    """A message from the platform, shown as a banner while it runs."""

    class Audience(models.TextChoices):
        EVERYONE = "everyone", "Everyone signed in"
        TEAMS = "teams", "Organization teams"
        OWNERS = "owners", "Organization owners"
        CUSTOMERS = "customers", "Customers (portal)"

    class Level(models.TextChoices):
        INFO = "info", "Information"
        WARNING = "warning", "Warning"

    title = models.CharField(max_length=120)
    body = models.TextField(max_length=1000, blank=True)
    audience = models.CharField(max_length=20, choices=Audience.choices, default=Audience.TEAMS)
    level = models.CharField(max_length=20, choices=Level.choices, default=Level.INFO)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-starts_at",)

    def __str__(self) -> str:
        return self.title

    def is_running(self, now=None) -> bool:
        now = now or timezone.now()
        return (
            self.is_active
            and self.starts_at <= now
            and (self.ends_at is None or now < self.ends_at)
        )


class FeatureFlag(BaseUUIDModel):
    """A switch for a feature: on or off for everyone, with per-organization overrides."""

    key = models.SlugField(max_length=80, unique=True)
    description = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=False)

    class Meta:
        ordering = ("key",)

    def __str__(self) -> str:
        return self.key


class FeatureFlagOverride(BaseUUIDModel):
    flag = models.ForeignKey(FeatureFlag, on_delete=models.CASCADE, related_name="overrides")
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="+"
    )
    enabled = models.BooleanField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("flag", "organization"), name="one_override_per_org")
        ]


class ImpersonationSession(BaseUUIDModel):
    """A platform admin acting as a user, for a limited time, with a reason. While it runs,
    audit rows record the admin as ``impersonator``."""

    class EndReason(models.TextChoices):
        ENDED = "ended", "Ended by the admin"
        EXPIRED = "expired", "Time ran out"
        REVOKED = "revoked", "No longer allowed"

    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="impersonations"
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="impersonated_sessions"
    )
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=255)
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=20, choices=EndReason.choices, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ("-started_at",)
        indexes = [models.Index(fields=("admin", "-started_at"))]

    def is_live(self, now=None) -> bool:
        now = now or timezone.now()
        return self.ended_at is None and now < self.expires_at
