"""Writes to a provider's working hours: the weekly pattern and special days. The staff pages
call these; they don't save WeeklyAvailability or AvailabilityException rows themselves.

- ``set_weekly_hours`` replaces the whole weekly pattern at once (what the editor shows is what
  is saved), under the provider's calendar lock (the one bookings take).
- ``hours_conflicts`` lists upcoming appointments that new weekly hours would leave outside
  working time. Nothing moves them; the editor warns before saving.
- ``set_special_day`` / ``delete_special_day``: one date off, or with different hours, without
  touching the weekly pattern. One per date: a new one replaces the old.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from bookings.models import Booking
from core.audit import AuditAction, record_audit
from core.exceptions import DomainError
from locations.models import Location, Weekday
from scheduling.availability import ACTIVE_STATUSES, zone_of
from scheduling.models import AvailabilityException, WeeklyAvailability
from staff.models import StaffProfile

MAX_PERIODS_PER_DAY = 4
CONFLICT_HORIZON_DAYS = 90
DAY_NAMES = dict(Weekday.choices)


def _lock_staff(staff: StaffProfile) -> StaffProfile:
    return StaffProfile.objects.select_for_update().get(pk=staff.pk)


def _summary(staff: StaffProfile) -> list[str]:
    rows = WeeklyAvailability.objects.filter(staff=staff, is_active=True).select_related("location")
    return [
        f"{DAY_NAMES[row.day_of_week][:3]} {row.start_time:%H:%M}-{row.end_time:%H:%M}"
        + (f" {row.location.name}" if row.location else "")
        for row in sorted(rows, key=lambda r: (r.day_of_week, r.start_time))
    ]


def check_periods(staff: StaffProfile, periods) -> list[tuple[int, time, time, Location | None]]:
    """``(weekday, start, end, location)`` sorted, or ``DomainError``: real weekdays, start
    before end, places the person works at, and no overlaps on a day."""
    allowed = {location.pk for location in staff.locations.all()}
    by_day: dict[int, list] = defaultdict(list)
    for weekday, start, end, location in periods:
        if weekday not in range(7):
            raise DomainError("Weekday must be 0 (Monday) to 6 (Sunday).", code="invalid_weekday")
        if start >= end:
            raise DomainError(
                f"On {DAY_NAMES[weekday]} the end must be after the start.", code="invalid_hours"
            )
        if location is not None and location.pk not in allowed:
            raise DomainError(
                f"{location.name} isn't one of this person's locations.", code="invalid_location"
            )
        by_day[weekday].append((start, end, location))
    cleaned = []
    for weekday, day_periods in sorted(by_day.items()):
        day_periods.sort(key=lambda item: item[0])
        if len(day_periods) > MAX_PERIODS_PER_DAY:
            raise DomainError(
                f"At most {MAX_PERIODS_PER_DAY} periods a day.", code="too_many_periods"
            )
        for first, second in zip(day_periods, day_periods[1:], strict=False):
            if second[0] < first[1]:
                raise DomainError(
                    f"The periods on {DAY_NAMES[weekday]} overlap.", code="overlapping"
                )
        cleaned += [(weekday, start, end, location) for start, end, location in day_periods]
    return cleaned


def hours_conflicts(staff: StaffProfile, periods, *, now: datetime | None = None) -> list:
    """Upcoming appointments (the next ``CONFLICT_HORIZON_DAYS``) that don't fit inside
    ``periods`` on their weekday, read on their location's wall clock. A period for any
    location covers every location; one for a location covers appointments there."""
    now = now or timezone.now()
    by_day: dict[int, list] = defaultdict(list)
    for weekday, start, end, location in periods:
        by_day[weekday].append((start, end, location.pk if location else None))
    bookings = (
        Booking.objects.filter(
            staff=staff,
            status__in=ACTIVE_STATUSES,
            start_datetime__gte=now,
            start_datetime__lt=now + timedelta(days=CONFLICT_HORIZON_DAYS),
        )
        .select_related("location", "service")
        .order_by("start_datetime")
    )
    outside = []
    for booking in bookings:
        zone = zone_of(booking.location) if booking.location else timezone.get_current_timezone()
        start = booking.start_datetime.astimezone(zone)
        end = booking.end_datetime.astimezone(zone)
        fits = end.date() == start.date() and any(
            (where is None or where == booking.location_id)
            and period_start <= start.time()
            and end.time() <= period_end
            for period_start, period_end, where in by_day[start.weekday()]
        )
        if not fits:
            outside.append(booking)
    return outside


@transaction.atomic
def set_weekly_hours(*, staff: StaffProfile, periods, actor=None) -> list[WeeklyAvailability]:
    """Replace the provider's weekly hours with ``periods``: ``(weekday, start, end,
    location or None)``. An empty list means they aren't working on any day."""
    staff = _lock_staff(staff)
    cleaned = check_periods(staff, periods)
    before = _summary(staff)
    WeeklyAvailability.objects.filter(staff=staff).delete()
    rows = WeeklyAvailability.objects.bulk_create(
        WeeklyAvailability(
            organization_id=staff.organization_id,
            staff=staff,
            day_of_week=weekday,
            start_time=start,
            end_time=end,
            location=location,
        )
        for weekday, start, end, location in cleaned
    )
    after = _summary(staff)
    if before != after:
        record_audit(
            AuditAction.AVAILABILITY_UPDATED,
            organization=staff.organization,
            actor=actor,
            target=staff,
            changes={"weekly_hours": [before, after]},
        )
    return rows


@transaction.atomic
def set_special_day(
    *,
    staff: StaffProfile,
    day: date,
    off_all_day: bool,
    start: time | None = None,
    end: time | None = None,
    reason: str = "",
    actor=None,
) -> AvailabilityException:
    """One date off, or working only ``start``-``end`` that day. Replaces an earlier special
    day on the same date."""
    staff = _lock_staff(staff)
    if day < timezone.localdate():
        raise DomainError("That day has already passed.", code="in_past")
    if not off_all_day:
        if start is None or end is None:
            raise DomainError("Enter the hours they work that day.", code="invalid_hours")
        if start >= end:
            raise DomainError("The end must be after the start.", code="invalid_hours")
    else:
        start = end = None
    AvailabilityException.objects.filter(staff=staff, date=day).delete()
    entry = AvailabilityException.objects.create(
        organization_id=staff.organization_id,
        staff=staff,
        date=day,
        unavailable_all_day=off_all_day,
        start_time=start,
        end_time=end,
        reason=reason[: AvailabilityException._meta.get_field("reason").max_length],
    )
    record_audit(
        AuditAction.AVAILABILITY_EXCEPTION_CREATED,
        organization=staff.organization,
        actor=actor,
        target=entry,
        metadata={
            "staff": str(staff.pk),
            "date": day.isoformat(),
            "hours": "off" if off_all_day else f"{start:%H:%M}-{end:%H:%M}",
        },
    )
    return entry


@transaction.atomic
def delete_special_day(*, entry: AvailabilityException, actor=None) -> None:
    _lock_staff(entry.staff)
    record_audit(
        AuditAction.AVAILABILITY_EXCEPTION_DELETED,
        organization=entry.organization,
        actor=actor,
        target=entry,
        metadata={"staff": str(entry.staff_id), "date": entry.date.isoformat()},
    )
    entry.delete()
