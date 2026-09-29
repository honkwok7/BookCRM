"""Services and their categories.

Write through ``services.services``: unique slugs, the plan's service limit, validation and
auditing. ``is_public`` means "bookable online" (the UI says so); the plan's proposed
``online_bookable`` flag would have duplicated it.
"""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.template.defaultfilters import slugify

from core.models import BaseUUIDModel
from crm.models import HEX_COLOR


class ServiceCategory(BaseUUIDModel):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="service_categories",
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=150)
    color = models.CharField(max_length=7, blank=True, validators=[HEX_COLOR])
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        unique_together = ("organization", "slug")
        ordering = ["sort_order", "name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class Service(BaseUUIDModel):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="services",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="services",
    )
    price = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=10, default="USD")
    duration_minutes = models.PositiveIntegerField()
    buffer_before_minutes = models.PositiveIntegerField(default=0)
    buffer_after_minutes = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_public = models.BooleanField(default=True)
    is_archived = models.BooleanField(default=False)
    image = models.ImageField(upload_to="services/images/", null=True, blank=True)
    color = models.CharField(max_length=20, blank=True)
    max_advance_days = models.PositiveIntegerField(default=30)
    min_notice_minutes = models.PositiveIntegerField(default=60)
    cancellation_deadline_hours = models.PositiveIntegerField(default=24)
    rescheduling_deadline_hours = models.PositiveIntegerField(default=24)
    capacity = models.PositiveIntegerField(default=1)
    # Empty: offered at every location. Otherwise only at these.
    locations = models.ManyToManyField("locations.Location", blank=True, related_name="services")
    # Only staff whose provider_type matches (ignoring case) can offer it. Empty: anyone.
    required_provider_type = models.CharField(max_length=60, blank=True)
    # A percentage, e.g. 13.00. A placeholder until a tax model exists.
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    cancellation_policy = models.TextField(blank=True)
    assigned_staff_members = models.ManyToManyField(
        "staff.StaffProfile",
        blank=True,
        related_name="assigned_services",
    )

    class Meta:
        unique_together = ("organization", "slug")
        ordering = ["name"]
        indexes = [
            models.Index(fields=["organization", "is_active", "is_public"]),
            models.Index(fields=["organization", "is_archived"]),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name
