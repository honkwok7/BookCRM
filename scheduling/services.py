from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bookings.models import Booking
from scheduling.models import (
    AvailabilityException,
    OrganizationHoliday,
    TimeOff,
    WeeklyAvailability,
)


def generate_slots(*, organization, service, staff_profile, date, step_minutes=15):
    tz = ZoneInfo(organization.timezone)
    weekday = date.weekday()

    availabilities = WeeklyAvailability.objects.filter(
        organization=organization,
        staff=staff_profile,
        day_of_week=weekday,
        is_active=True,
    ).order_by("start_time")

    if not availabilities.exists():
        return []

    holiday = OrganizationHoliday.objects.filter(
        organization=organization, date=date, full_day_closure=True
    ).exists()
    if holiday:
        return []

    exception = AvailabilityException.objects.filter(
        organization=organization, staff=staff_profile, date=date
    ).first()
    if exception and exception.unavailable_all_day:
        return []

    existing = Booking.objects.filter(
        organization=organization,
        staff=staff_profile,
        start_datetime__date=date,
        status__in=[
            Booking.Status.PENDING,
            Booking.Status.CONFIRMED,
            Booking.Status.CHECKED_IN,
            Booking.Status.IN_PROGRESS,
        ],
    )

    slots = []
    now = datetime.now(tz)
    for availability in availabilities:
        start_dt = datetime.combine(date, availability.start_time, tzinfo=tz)
        end_dt = datetime.combine(date, availability.end_time, tzinfo=tz)

        if exception and exception.start_time and exception.end_time:
            start_dt = max(start_dt, datetime.combine(date, exception.start_time, tzinfo=tz))
            end_dt = min(end_dt, datetime.combine(date, exception.end_time, tzinfo=tz))

        cursor = start_dt
        while cursor + timedelta(minutes=service.duration_minutes) <= end_dt:
            slot_start = cursor
            slot_end = cursor + timedelta(minutes=service.duration_minutes)

            if slot_start <= now + timedelta(minutes=service.min_notice_minutes):
                cursor += timedelta(minutes=step_minutes)
                continue

            if slot_start > now + timedelta(days=service.max_advance_days):
                cursor += timedelta(minutes=step_minutes)
                continue

            has_time_off = TimeOff.objects.filter(
                organization=organization,
                staff=staff_profile,
                approval_status="approved",
                start_datetime__lt=slot_end,
                end_datetime__gt=slot_start,
            ).exists()
            if has_time_off:
                cursor += timedelta(minutes=step_minutes)
                continue

            overlap = existing.filter(
                start_datetime__lt=slot_end, end_datetime__gt=slot_start
            ).exists()
            if overlap:
                cursor += timedelta(minutes=step_minutes)
                continue

            slots.append(slot_start)
            cursor += timedelta(minutes=step_minutes)

    return slots
