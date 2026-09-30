"""The provider's own day (M5.3): what ``/staff/dashboard/`` shows.

Everything is scoped to one staff profile, so a provider never sees another provider's
appointments here, whatever their role. Fixed number of queries (4).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from bookings.models import Booking
from core.web import organization_zone
from scheduling.availability import ACTIVE_STATUSES
from scheduling.models import TimeOff

AHEAD_DAYS = 7
SEEN_DAYS = 30
CANCELLED_DAYS = 7
LIST_LIMIT = 5


def greeting(local: datetime) -> str:
    if local.hour < 12:
        return "Good morning"
    if local.hour < 18:
        return "Good afternoon"
    return "Good evening"


def _midnight(zone, day) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone)


def provider_day(profile, *, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    zone = organization_zone(profile.organization)
    local = now.astimezone(zone)
    today = local.date()
    day_start, day_end = _midnight(zone, today), _midnight(zone, today + timedelta(days=1))
    week_start = _midnight(zone, today - timedelta(days=today.weekday()))
    week_end = _midnight(zone, today - timedelta(days=today.weekday()) + timedelta(days=7))
    ahead_end = _midnight(zone, today + timedelta(days=AHEAD_DAYS))
    mine = Booking.objects.filter(organization=profile.organization, staff=profile)

    # 1 query: today and the next days, still on (today's finished ones stay for the record).
    coming = list(
        mine.filter(start_datetime__gte=day_start, start_datetime__lt=ahead_end)
        .exclude(status__in=(Booking.Status.CANCELLED, Booking.Status.REJECTED))
        .select_related("service", "customer", "location")
        .order_by("start_datetime")
    )
    today_rows = [b for b in coming if b.start_datetime < day_end]
    later = [b for b in coming if b.start_datetime >= day_end and b.status in ACTIVE_STATUSES]
    upcoming = [b for b in coming if b.status in ACTIVE_STATUSES and b.end_datetime > now]
    # 1 query: this week's count and the customers seen lately.
    totals = mine.aggregate(
        week=Count(
            "pk",
            filter=Q(
                start_datetime__gte=week_start,
                start_datetime__lt=week_end,
                status__in=(*ACTIVE_STATUSES, Booking.Status.COMPLETED),
            ),
        ),
        seen=Count(
            "customer",
            distinct=True,
            filter=Q(
                status=Booking.Status.COMPLETED,
                start_datetime__gte=now - timedelta(days=SEEN_DAYS),
            ),
        ),
    )
    # 1 query: recently cancelled appointments of theirs.
    cancelled = list(
        mine.filter(
            status=Booking.Status.CANCELLED,
            cancelled_at__gte=now - timedelta(days=CANCELLED_DAYS),
        )
        .select_related("service")
        .order_by("-cancelled_at")[:LIST_LIMIT]
    )
    # 1 query: their time off and blocked time from now on (requests included).
    time_off = list(
        TimeOff.objects.filter(
            staff=profile,
            end_datetime__gt=now,
            approval_status__in=(TimeOff.ApprovalStatus.PENDING, TimeOff.ApprovalStatus.APPROVED),
        ).order_by("start_datetime")[:LIST_LIMIT]
    )
    return {
        "now": now,
        "zone": zone,
        "today": today,
        "greeting": greeting(local),
        "name": profile.user.first_name or profile.public_name,
        "today_rows": today_rows,
        "next": next((b for b in upcoming if b.start_datetime > now), None),
        "current": next((b for b in upcoming if b.start_datetime <= now), None),
        "later": later,
        "week_count": totals["week"] or 0,
        "seen_count": totals["seen"] or 0,
        "cancelled": cancelled,
        "time_off": time_off,
        "seen_days": SEEN_DAYS,
        "cancelled_days": CANCELLED_DAYS,
    }
