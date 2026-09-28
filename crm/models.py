from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

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
