"""When providers are working, for shading the calendar (bookings/calendar.py).

This is the first part of the availability engine's rule (scheduling/availability.py), without
a service and without appointments, which the calendar shows anyway:

1. weekly hours for that weekday (rows for the chosen location or for any location; with no
   location chosen, every row);
2. limited by availability exceptions for that date (off all day, or only certain times);
3. limited by the location's opening hours, when it has any;
4. minus location closures and organization holidays;
5. minus approved time off.

Times are read on the wall clock of the location each weekly row belongs to (a row for any
location uses the chosen location, else the organization's default location). Everything is
loaded in a fixed number of queries, however many providers and days are shown.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

from django.db.models import Q

from locations.models import Location, LocationClosure, LocationHours
from scheduling.availability import Interval, intersect, merge, subtract, zone_of
from scheduling.models import (
    AvailabilityException,
    OrganizationHoliday,
    TimeOff,
    WeeklyAvailability,
)


def _at(day: date, value: time, zone) -> datetime:
    return datetime.combine(day, value, tzinfo=zone).astimezone(UTC)


def working_hours(
    organization, providers, first_day: date, last_day: date, *, location=None
) -> dict[tuple, list[Interval]]:
    """``{(staff_pk, day): [(start, end), ...]}`` in UTC for each provider and each day from
    ``first_day`` to ``last_day`` (inclusive). Days without working time are left out."""
    staff_ids = [provider.pk for provider in providers]
    if not staff_ids or last_day < first_day:
        return {}
    locations = {row.pk: row for row in Location.objects.filter(organization=organization)}
    fallback = location or next((row for row in locations.values() if row.is_default), None)
    if fallback is None:
        return {}

    weekly = defaultdict(list)
    rows = WeeklyAvailability.objects.filter(staff_id__in=staff_ids, is_active=True)
    if location is not None:
        rows = rows.filter(Q(location=location) | Q(location__isnull=True))
    for row in rows:
        place = locations.get(row.location_id) or fallback
        weekly[(row.staff_id, row.day_of_week)].append((place, row.start_time, row.end_time))

    exceptions = defaultdict(list)
    for row in AvailabilityException.objects.filter(
        staff_id__in=staff_ids, date__gte=first_day, date__lte=last_day
    ):
        exceptions[(row.staff_id, row.date)].append(row)

    opening = defaultdict(list)
    for row in LocationHours.objects.filter(location__organization=organization):
        opening[(row.location_id, row.weekday)].append((row.opens_at, row.closes_at))
    has_hours = {location_id for location_id, _ in opening}

    closures = defaultdict(list)
    for row in LocationClosure.objects.filter(
        location__organization=organization, start_date__lte=last_day, end_date__gte=first_day
    ):
        closures[row.location_id].append(row)
    holidays = defaultdict(list)
    for row in OrganizationHoliday.objects.filter(
        organization=organization, date__gte=first_day, date__lte=last_day
    ):
        holidays[row.date].append(row)

    time_off = defaultdict(list)
    for row in TimeOff.objects.filter(
        staff_id__in=staff_ids,
        approval_status=TimeOff.ApprovalStatus.APPROVED,
        start_datetime__lt=_at(last_day + timedelta(days=2), time.min, UTC),
        end_datetime__gt=_at(first_day - timedelta(days=1), time.min, UTC),
    ):
        time_off[row.staff_id].append((row.start_datetime, row.end_datetime))

    result = {}
    day = first_day
    while day <= last_day:
        for staff_id in staff_ids:
            windows = []
            for place, start, end in weekly[(staff_id, day.weekday())]:
                zone = zone_of(place)
                window = [(_at(day, start, zone), _at(day, end, zone))]
                if place.pk in has_hours:
                    window = intersect(
                        window,
                        [
                            (_at(day, opens, zone), _at(day, closes, zone))
                            for opens, closes in opening[(place.pk, day.weekday())]
                        ],
                    )
                closed = []
                for closure in closures[place.pk]:
                    if closure.start_date <= day <= closure.end_date:
                        if closure.all_day:
                            window = []
                            break
                        closed.append(
                            (_at(day, closure.start_time, zone), _at(day, closure.end_time, zone))
                        )
                windows.extend(subtract(window, closed))
            if not windows:
                continue
            zone = zone_of(fallback)
            rows = exceptions[(staff_id, day)]
            if any(row.unavailable_all_day for row in rows):
                continue
            if rows:
                windows = intersect(
                    merge(windows),
                    [
                        (_at(day, row.start_time, zone), _at(day, row.end_time, zone))
                        for row in rows
                        if row.start_time and row.end_time
                    ],
                )
            closed = []
            for holiday in holidays[day]:
                if holiday.full_day_closure or not (holiday.start_time and holiday.end_time):
                    windows = []
                    break
                closed.append(
                    (_at(day, holiday.start_time, zone), _at(day, holiday.end_time, zone))
                )
            windows = subtract(merge(windows), closed + time_off[staff_id])
            if windows:
                result[(staff_id, day)] = windows
        day += timedelta(days=1)
    return result
