"""Locations: the places where an organization sees customers.

Every organization has exactly one *default* location (created with the organization, see
``locations.signals``), which must stay active. Write through ``locations.services``: it
applies the plan limit, keeps a single default, validates hours and closures, and audits.
"""

from __future__ import annotations

import zoneinfo

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from core.models import BaseUUIDModel


class Weekday(models.IntegerChoices):
    # Python's date.weekday() numbering, like scheduling.WeeklyAvailability.day_of_week.
    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3, "Thursday"
    FRIDAY = 4, "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


def validate_timezone(value: str) -> None:
    if value not in zoneinfo.available_timezones():
        raise ValidationError(f"“{value}” is not a known time zone.", code="invalid_timezone")


class Location(BaseUUIDModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="locations"
    )
    name = models.CharField(max_length=120)
    # Stable once created: it will appear in public booking links (M4.6).
    slug = models.SlugField(max_length=140)
    is_default = models.BooleanField(default=False)
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, blank=True)
    region = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, blank=True)
    timezone = models.CharField(max_length=64, default="UTC", validators=[validate_timezone])
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    booking_enabled = models.BooleanField(default=True)
    # Reserved for per-location online booking rules (M4.6); not editable yet.
    booking_settings = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-is_default", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"], name="location_unique_slug_per_org"
            ),
            models.UniqueConstraint(
                fields=["organization"],
                condition=Q(is_default=True),
                name="location_one_default_per_org",
            ),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(is_active=True),
                name="location_default_is_active",
            ),
        ]
        indexes = [models.Index(fields=["organization", "is_active"])]

    def __str__(self) -> str:
        return self.name

    @property
    def address_lines(self) -> list[str]:
        locality = " ".join(
            part
            for part in (
                ", ".join(p for p in (self.city, self.region) if p),
                self.postal_code,
                self.country,
            )
            if part
        )
        return [line for line in (self.address_line1, self.address_line2, locality) if line]


class LocationHours(BaseUUIDModel):
    """One opening period on a weekday, in the location's time zone.

    A day may have up to two periods (e.g. closed for lunch). A location with no hours at all
    has not set any, and does not restrict bookings; a day without periods is closed.
    """

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="+"
    )
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name="hours")
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    opens_at = models.TimeField()
    closes_at = models.TimeField()

    class Meta:
        ordering = ("weekday", "opens_at")
        constraints = [
            models.CheckConstraint(
                condition=Q(weekday__gte=0, weekday__lte=6), name="location_hours_valid_weekday"
            ),
            models.CheckConstraint(
                condition=Q(opens_at__lt=F("closes_at")), name="location_hours_open_before_close"
            ),
            models.UniqueConstraint(
                fields=["location", "weekday", "opens_at"], name="location_hours_unique_start"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_weekday_display()} {self.opens_at:%H:%M}-{self.closes_at:%H:%M}"


class LocationClosure(BaseUUIDModel):
    """The location is closed on these dates (all day, or between two times each day).

    Organization-wide holidays stay in ``scheduling.OrganizationHoliday``.
    """

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="+"
    )
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name="closures")
    start_date = models.DateField()
    end_date = models.DateField()
    all_day = models.BooleanField(default=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ("start_date", "start_time")
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__gte=F("start_date")), name="location_closure_date_order"
            ),
            models.CheckConstraint(
                condition=Q(all_day=True, start_time__isnull=True, end_time__isnull=True)
                | Q(
                    all_day=False,
                    start_time__isnull=False,
                    end_time__isnull=False,
                    start_time__lt=F("end_time"),
                ),
                name="location_closure_times",
            ),
        ]
        indexes = [models.Index(fields=["location", "end_date"])]

    def __str__(self) -> str:
        return f"{self.location} closed {self.start_date}–{self.end_date}"
