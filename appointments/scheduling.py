"""Shared interval rules for the existing local date/time scheduling API."""

from datetime import datetime, timedelta

from appointments.models import Appointment, AppointmentStatus
from specialists.models import WorkingHour


def appointment_interval(date, time, duration):
    """Return a half-open [start, end) interval, with duration in minutes."""
    start = datetime.combine(date, time)
    return start, start + timedelta(minutes=duration)


def scheduling_context(specialist, date, *, exclude_pk=None):
    """Load intervals once, so availability can validate many candidates."""
    working = [
        (datetime.combine(date, start), datetime.combine(date, end))
        for start, end in WorkingHour.objects.filter(
            specialist=specialist, day=date.weekday()
        ).values_list("start_time", "end_time")
    ]
    active = Appointment.objects.filter(
        specialist=specialist,
        date__lte=date,
        status__in=[AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED],
    )
    if exclude_pk is not None:
        active = active.exclude(pk=exclude_pk)
    # Earlier starts may still occupy this day (legacy appointments can cross
    # midnight). There is no schema-level maximum duration to bound the lookback.
    occupied = []
    for day, time, duration in active.values_list("date", "time", "duration"):
        start, end = appointment_interval(day, time, duration)
        if end.date() >= date:
            occupied.append((start, end))
    return working, occupied


def scheduling_error(date, time, duration, slot_duration, working, occupied):
    """Return an interval validation error, or None when the slot is valid.

    Endpoint-specific past-date/time checks remain with their callers. All
    comparisons here use the project's local, timezone-free date/time values.
    """
    if duration <= 0:
        return "Appointment duration must be positive."
    try:
        start, end = appointment_interval(date, time, duration)
    except OverflowError:
        return "Appointment duration exceeds the supported date range."

    fitting = [wh_start for wh_start, wh_end in working if wh_start <= start < end <= wh_end]
    if not fitting:
        return "Selected time is outside specialist working hours."

    if (
        time.second
        or time.microsecond
        or slot_duration <= 0
        or not any(
            (start - wh_start) % timedelta(minutes=slot_duration) == timedelta(0)
            for wh_start in fitting
        )
    ):
        return f"Selected time does not align with the {slot_duration}-minute slot grid."

    if any(other_start < end and other_end > start for other_start, other_end in occupied):
        return "This slot overlaps an existing appointment."
    return None
