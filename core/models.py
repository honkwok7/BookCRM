import uuid

from django.conf import settings
from django.db import models


class BaseUUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditLogImmutableError(Exception):
    """Audit rows are append-only."""


class AuditLog(BaseUUIDModel):
    """Append-only record of an important action. Write through ``core.audit.record_audit``."""

    class ActorType(models.TextChoices):
        USER = "user", "User"
        SYSTEM = "system", "System"
        PLATFORM_ADMIN = "platform_admin", "Platform admin"
        API_KEY = "api_key", "API key"
        AI_AGENT = "ai_agent", "AI agent"

    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    actor_type = models.CharField(
        max_length=20, choices=ActorType.choices, default=ActorType.SYSTEM
    )
    # Set when a platform admin acts while impersonating ``user`` (M5.5).
    impersonator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="impersonated_audit_logs",
    )
    action = models.CharField(max_length=120)
    object_type = models.CharField(max_length=120, blank=True)
    object_identifier = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action"]),
            models.Index(fields=["object_type", "object_identifier"]),
            models.Index(fields=["organization", "-created_at"]),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditLogImmutableError("Audit log entries cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutableError("Audit log entries cannot be deleted.")

    def __str__(self) -> str:
        return f"{self.action} ({self.object_type}:{self.object_identifier})"
