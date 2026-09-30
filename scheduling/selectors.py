"""Schedule reads shared by the staff pages and the provider area."""

from __future__ import annotations

from locations.models import Weekday
from scheduling.models import WeeklyAvailability


def weekly_hours(staff) -> dict:
    """``{"week": [{"weekday", "label", "periods": [(start, end, where), ...]}, ...],
    "has_availability": bool}`` for one provider's active weekly hours."""
    periods: dict[int, list] = {day: [] for day in range(7)}
    rows = WeeklyAvailability.objects.filter(staff=staff, is_active=True).select_related("location")
    for row in rows:
        where = row.location.name if row.location else "Any of their locations"
        periods[row.day_of_week].append((row.start_time, row.end_time, where))
    week = [
        {"weekday": day, "label": label, "periods": sorted(periods[day])}
        for day, label in Weekday.choices
    ]
    return {"week": week, "has_availability": any(periods.values())}
