"""Staff (providers): who works at which location and offers which services.

Write through ``staff.services``: it checks membership, the plan's staff limit, that
locations and services belong to the same organization, and audits. ``StaffServiceOffering``
is the source of truth for "who offers what, where"; ``Service.assigned_staff_members`` is a
transitional mirror of it (staff with any active offering), kept in sync by the service
layer until M11.3 removes it.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseUUIDModel


class StaffProfile(BaseUUIDModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_profiles"
    )
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="staff_profiles"
    )
    # The name customers see. Empty means the user's full name.
    display_name = models.CharField(max_length=120, blank=True)
    job_title = models.CharField(max_length=120, blank=True)
    # A free-form kind of provider ("Massage therapist"); services may require one (M3.3).
    provider_type = models.CharField(max_length=60, blank=True)
    bio = models.TextField(blank=True)
    profile_image = models.ImageField(upload_to="staff/profiles/", blank=True, null=True)
    phone_number = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    is_accepting_bookings = models.BooleanField(default=True)
    # Listed on the public booking page. Off: the team can still book them at reception.
    online_booking_visible = models.BooleanField(default=True)
    # Empty means no daily limit. Enforced by the booking engine from M4.1.
    max_daily_appointments = models.PositiveSmallIntegerField(null=True, blank=True)
    appointment_color = models.CharField(max_length=20, blank=True)
    locations = models.ManyToManyField(
        "locations.Location", blank=True, related_name="staff_members"
    )

    class Meta:
        unique_together = ("organization", "user")
        indexes = [models.Index(fields=["organization", "is_active", "is_accepting_bookings"])]

    def __str__(self) -> str:
        return f"{self.user} @ {self.organization}"

    @property
    def full_name(self) -> str:
        return self.user.get_full_name() or self.user.email

    @property
    def public_name(self) -> str:
        """The name safe to show to the public: never the email address."""
        return self.display_name or self.user.get_full_name() or self.job_title or "Team member"


class StaffServiceOffering(BaseUUIDModel):
    """``staff`` offers ``service`` at ``location``, or at all their locations when it is empty,
    optionally with their own duration and price."""

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="+"
    )
    staff = models.ForeignKey(StaffProfile, on_delete=models.CASCADE, related_name="offerings")
    service = models.ForeignKey(
        "services.Service", on_delete=models.CASCADE, related_name="offerings"
    )
    location = models.ForeignKey(
        "locations.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="offerings",
    )
    custom_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    custom_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("service__name",)
        constraints = [
            models.UniqueConstraint(
                fields=["staff", "service", "location"],
                condition=Q(location__isnull=False),
                name="offering_unique_per_location",
            ),
            models.UniqueConstraint(
                fields=["staff", "service"],
                condition=Q(location__isnull=True),
                name="offering_unique_all_locations",
            ),
            models.CheckConstraint(
                condition=Q(custom_duration_minutes__isnull=True)
                | Q(custom_duration_minutes__gt=0),
                name="offering_positive_duration",
            ),
            models.CheckConstraint(
                condition=Q(custom_price__isnull=True) | Q(custom_price__gte=0),
                name="offering_price_not_negative",
            ),
        ]
        indexes = [models.Index(fields=["organization", "service", "is_active"])]

    def __str__(self) -> str:
        where = self.location.name if self.location_id else "all locations"
        return f"{self.staff} offers {self.service} at {where}"

    @property
    def duration_minutes(self) -> int:
        return self.custom_duration_minutes or self.service.duration_minutes

    @property
    def price(self):
        return self.custom_price if self.custom_price is not None else self.service.price
