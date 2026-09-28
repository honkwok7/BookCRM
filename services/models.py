from django.db import models
from django.template.defaultfilters import slugify

from core.models import BaseUUIDModel


class ServiceCategory(BaseUUIDModel):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="service_categories",
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=150)

    class Meta:
        unique_together = ("organization", "slug")
        ordering = ["name"]

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
