"""The front desk's view of today (M5.2): what ``/app/reception/`` shows.

``front_desk(organization, location, now)`` returns today's timeline, who is waiting (checked
in), what every provider is doing now, today's cancellations and the waitlist count, in a fixed
number of queries (the page refreshes itself every 30 seconds). ``next_free_times`` finds the
next free time per service; it runs the availability engine per service, so the page loads it
separately and it is cached for a minute.

"Now" for a provider is simple on purpose: with someone (an appointment on now), off (time off
now, or outside their weekly hours today), otherwise free. Closures and one-day exceptions are
left to the booking forms, which check everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from bookings.models import Booking, WaitlistEntry
from scheduling.availability import AvailabilityService, zone_of
from scheduling.models import TimeOff, WeeklyAvailability
from services.selectors import services_bookable_at
from staff.models import StaffProfile

ON = (
    Booking.Status.PENDING,
    Booking.Status.CONFIRMED,
    Booking.Status.CHECKED_IN,
    Booking.Status.IN_PROGRESS,
)
WITH_SOMEONE = (Booking.Status.CHECKED_IN, Booking.Status.IN_PROGRESS)
NEXT_FREE_SERVICES = 8
NEXT_FREE_DAYS = 7
NEXT_FREE_CACHE_SECONDS = 60


@dataclass
class ProviderNow:
    staff: StaffProfile
    state: str  # "busy", "free" or "off"
    current: Booking | None = None
    next: Booking | None = None


def _day_bounds(zone, day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start, datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)


def front_desk(organization, location, *, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    zone = zone_of(location)
    today = now.astimezone(zone).date()
    day_start, day_end = _day_bounds(zone, today)

    appointments = Booking.objects.filter(organization=organization, location=location)
    # 1 query: today's appointments (everything but rejected requests).
    timeline = list(
        appointments.filter(start_datetime__gte=day_start, start_datetime__lt=day_end)
        .exclude(status=Booking.Status.REJECTED)
        .select_related("service", "staff__user", "customer")
        .order_by("start_datetime", "staff__display_name")
    )
    # 1 query: cancelled today (whenever the appointment was for).
    cancellations = list(
        appointments.filter(
            status=Booking.Status.CANCELLED,
            cancelled_at__gte=day_start,
            cancelled_at__lt=day_end,
        )
        .select_related("service", "staff__user")
        .order_by("-cancelled_at")[:20]
    )
    # 1 query: people waiting for a time here.
    waitlist_count = (
        WaitlistEntry.objects.filter(organization=organization, status=WaitlistEntry.Status.WAITING)
        .filter(Q(location__isnull=True) | Q(location=location))
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .count()
    )
    waiting = sorted(
        (b for b in timeline if b.status == Booking.Status.CHECKED_IN),
        key=lambda b: b.checked_in_at or b.start_datetime,
    )
    return {
        "today": today,
        "zone": zone,
        "timeline": timeline,
        "waiting": waiting,
        "cancellations": cancellations,
        "waitlist_count": waitlist_count,
        "providers": providers_now(organization, location, timeline, now=now, zone=zone),
        "now": now,
    }


def providers_now(organization, location, timeline, *, now, zone) -> list[ProviderNow]:
    """What each provider working here is doing now (3 queries)."""
    staff = list(
        StaffProfile.objects.filter(
            organization=organization,
            is_active=True,
            is_accepting_bookings=True,
            locations=location,
        )
        .select_related("user")
        .order_by("display_name", "user__first_name", "user__last_name")
    )
    ids = [person.pk for person in staff]
    local = now.astimezone(zone)
    working = set(
        WeeklyAvailability.objects.filter(
            staff_id__in=ids,
            is_active=True,
            day_of_week=local.weekday(),
            start_time__lte=local.time(),
            end_time__gt=local.time(),
        )
        .filter(Q(location__isnull=True) | Q(location=location))
        .values_list("staff_id", flat=True)
    )
    away = set(
        TimeOff.objects.filter(
            staff_id__in=ids,
            approval_status="approved",
            start_datetime__lte=now,
            end_datetime__gt=now,
        ).values_list("staff_id", flat=True)
    )
    board = []
    for person in staff:
        mine = [b for b in timeline if b.staff_id == person.pk and b.status in ON]
        current = next((b for b in mine if b.start_datetime <= now < b.end_datetime), None)
        upcoming = next((b for b in mine if b.start_datetime > now), None)
        if current is not None:
            state = "busy"
        elif person.pk in away or person.pk not in working:
            state = "off"
        else:
            state = "free"
        board.append(ProviderNow(person, state, current, upcoming))
    return board


def next_free_times(organization, location, *, now: datetime | None = None) -> list[dict]:
    """The next free time for each bookable service here (the team's view: no online notice),
    within ``NEXT_FREE_DAYS``. Cached per organization and location for a minute."""
    now = now or timezone.now()
    key = f"reception:next-free:{organization.pk}:{location.pk}"
    rows = cache.get(key)
    if rows is not None:
        return rows
    today = now.astimezone(zone_of(location)).date()
    services = list(
        services_bookable_at(organization, location, public=False).order_by("name")[
            :NEXT_FREE_SERVICES
        ]
    )
    rows = []
    for service in services:
        engine = AvailabilityService(organization, service, location=location, now=now)
        slots = engine.get_available_slots(today, today + timedelta(days=NEXT_FREE_DAYS - 1))
        first = slots[0] if slots else None
        rows.append(
            {
                "service": service,
                "start": first.start if first else None,
                "staff": first.candidates[0].staff if first else None,
            }
        )
    cache.set(key, rows, NEXT_FREE_CACHE_SECONDS)
    return rows
