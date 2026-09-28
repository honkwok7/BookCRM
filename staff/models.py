from django.conf import settings
from django.db import models

from core.models import BaseUUIDModel


class StaffProfile(BaseUUIDModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_profiles"
    )
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="staff_profiles"
    )
    job_title = models.CharField(max_length=120, blank=True)
    bio = models.TextField(blank=True)
    profile_image = models.ImageField(upload_to="staff/profiles/", blank=True, null=True)
    phone_number = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    is_accepting_bookings = models.BooleanField(default=True)
    appointment_color = models.CharField(max_length=20, blank=True)

    class Meta:
        unique_together = ("organization", "user")
        indexes = [models.Index(fields=["organization", "is_active", "is_accepting_bookings"])]

    def __str__(self) -> str:
        return f"{self.user} @ {self.organization}"
