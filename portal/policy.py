"""What a customer may change online (M5.4). The booking services enforce the same rules
(``enforce_deadline`` / ``public=True``); this only decides which buttons to show and why."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from bookings.models import Booking
from bookings.services import ALLOWED_TRANSITIONS, RESCHEDULABLE_STATUSES


@dataclass(frozen=True)
class ChangePolicy:
    can_cancel: bool
    can_reschedule: bool
    reason: str = ""  # why not, when neither is possible for an upcoming appointment


def change_policy(booking: Booking, *, now: datetime | None = None) -> ChangePolicy:
    now = now or timezone.now()
    if booking.start_datetime <= now:
        return ChangePolicy(False, False)
    cancellable = Booking.Status.CANCELLED in ALLOWED_TRANSITIONS[booking.status]
    movable = booking.status in RESCHEDULABLE_STATUSES
    if not (cancellable or movable):
        return ChangePolicy(False, False)
    hours = booking.service.cancellation_deadline_hours
    if hours and booking.start_datetime - now < timedelta(hours=hours):
        return ChangePolicy(
            False,
            False,
            f"Appointments can't be changed online less than {hours} hours before they start. "
            f"Please contact {booking.organization.name}.",
        )
    return ChangePolicy(cancellable, movable)


def zone_for(booking: Booking) -> ZoneInfo:
    name = booking.location.timezone if booking.location else booking.organization.timezone
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def local_time(value: datetime, zone: ZoneInfo) -> datetime:
    """Wall-clock time in ``zone``, without tzinfo: the template date filters would otherwise
    convert an aware value back to the active time zone (one page can show several zones)."""
    return value.astimezone(zone).replace(tzinfo=None)


def localize(bookings):
    """Add ``local_start``, ``local_end`` and ``zone`` (the location's time zone) to each."""
    for booking in bookings:
        zone = zone_for(booking)
        booking.zone = zone
        booking.local_start = local_time(booking.start_datetime, zone)
        booking.local_end = local_time(booking.end_datetime, zone)
    return bookings
