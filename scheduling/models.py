from django.core.exceptions import ValidationError
from django.db import models

from core.models import BaseUUIDModel


class WeeklyAvailability(BaseUUIDModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="weekly_availabilities"
    )
    staff = models.ForeignKey(
        "staff.StaffProfile", on_delete=models.CASCADE, related_name="weekly_availabilities"
    )
    # Where these hours apply. Empty: at any location the person works at.
    location = models.ForeignKey(
        "locations.Location",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="weekly_availabilities",
    )
    day_of_week = models.PositiveSmallIntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["staff", "day_of_week", "start_time"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(day_of_week__gte=0, day_of_week__lte=6),
                name="weekly_availability_valid_day",
            ),
        ]

    def clean(self):
        if self.start_time >= self.end_time:
            raise ValidationError("start_time must be before end_time")


class AvailabilityException(BaseUUIDModel):
    """A change to one person's hours on one date. ``unavailable_all_day``: off that day.
    Otherwise the rows for a date (there may be several) are the only times they work that
    day, within their usual weekly hours."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="availability_exceptions",
    )
    staff = models.ForeignKey(
        "staff.StaffProfile", on_delete=models.CASCADE, related_name="availability_exceptions"
    )
    date = models.DateField()
    unavailable_all_day = models.BooleanField(default=False)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)

    def clean(self):
        if self.unavailable_all_day:
            return
        if not self.start_time or not self.end_time:
            raise ValidationError("start_time and end_time are required for partial availability")
        if self.start_time >= self.end_time:
            raise ValidationError("start_time must be before end_time")


class TimeOff(BaseUUIDModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="time_off_entries"
    )
    staff = models.ForeignKey(
        "staff.StaffProfile", on_delete=models.CASCADE, related_name="time_off_entries"
    )
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    approval_status = models.CharField(max_length=30, default="approved")

    def clean(self):
        if self.start_datetime >= self.end_datetime:
            raise ValidationError("start_datetime must be before end_datetime")


class OrganizationHoliday(BaseUUIDModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="holidays"
    )
    date = models.DateField()
    name = models.CharField(max_length=120)
    full_day_closure = models.BooleanField(default=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)

    class Meta:
        unique_together = ("organization", "date", "name")

    def clean(self):
        if self.full_day_closure:
            return
        if not self.start_time or not self.end_time:
            raise ValidationError("start_time and end_time are required for a partial closure")
        if self.start_time >= self.end_time:
            raise ValidationError("start_time must be before end_time")
