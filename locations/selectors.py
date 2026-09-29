"""Location reads, always scoped to one organization."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Prefetch
from django.utils import timezone

from locations.models import Location, LocationClosure, LocationHours, Weekday


def locations_for(organization, *, active_only: bool = False):
    locations = Location.objects.filter(organization=organization).prefetch_related(
        Prefetch("hours", queryset=LocationHours.objects.order_by("weekday", "opens_at"))
    )
    return locations.filter(is_active=True) if active_only else locations


def default_location(organization) -> Location | None:
    return Location.objects.filter(organization=organization, is_default=True).first()


def weekly_hours(location: Location) -> list[dict]:
    """Seven rows, Monday first: ``{"weekday", "label", "periods": [(opens, closes), ...]}``.

    Uses the prefetched ``hours`` when present.
    """
    periods: dict[int, list] = {day: [] for day in Weekday.values}
    for row in location.hours.all():
        periods[row.weekday].append((row.opens_at, row.closes_at))
    return [
        {"weekday": day, "label": label, "periods": sorted(periods[day])}
        for day, label in Weekday.choices
    ]


def local_today(location: Location) -> date:
    """Today's date where the location is."""
    try:
        zone = ZoneInfo(location.timezone)
    except ZoneInfoNotFoundError, ValueError:
        zone = ZoneInfo("UTC")
    return timezone.now().astimezone(zone).date()


def upcoming_closures(location: Location, *, today: date):
    """Closures that haven't ended yet, soonest first."""
    return location.closures.filter(end_date__gte=today).order_by("start_date", "start_time")


def past_closures(location: Location, *, today: date, limit: int = 10):
    return location.closures.filter(end_date__lt=today).order_by("-start_date")[:limit]


def closures_for(organization):
    return LocationClosure.objects.filter(organization=organization).select_related("location")
