"""The numbers behind the owner and manager dashboard (M5.1).

``dashboard_report(organization, filters)`` returns every analytics widget in a fixed number of
queries (conditional aggregation), whatever the amount of data, and caches the result for
``CACHE_SECONDS`` per organization and filter. The caller checks ``reports.view``.

Periods are calendar days in the organization's time zone. "Recent" is the last
``filters.days`` days up to today. Rates only count appointments whose time has passed, and
say so when there are fewer than ``MIN_SAMPLE`` of them: a rate of 1 in 3 means nothing.
Revenue is an estimate from completed appointments' price snapshots, not payments.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from bookings.models import Booking, Customer
from core.web import organization_zone

CACHE_SECONDS = 60
MIN_SAMPLE = 20
PERIODS = (7, 30, 90)
TOP = 5
INACTIVE = (Booking.Status.CANCELLED, Booking.Status.REJECTED)


@dataclass(frozen=True)
class Filters:
    """What the dashboard is narrowed to. ``location`` and ``staff`` are ids already checked
    to belong to the organization (or None for all)."""

    days: int = 30
    location: str | None = None
    staff: str | None = None

    def cache_key(self, organization, today: date) -> str:
        return (
            f"dashboard:{organization.pk}:{today.isoformat()}:"
            f"{self.days}:{self.location or '-'}:{self.staff or '-'}"
        )


def _midnight(zone, day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone)


def _rate(part: int, total: int) -> float | None:
    """A percentage, or None when there isn't enough data to mean anything."""
    if total < MIN_SAMPLE:
        return None
    return round(100 * part / total, 1)


def dashboard_report(organization, filters: Filters, *, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    zone = organization_zone(organization)
    today = now.astimezone(zone).date()
    key = filters.cache_key(organization, today)
    report = cache.get(key)
    if report is None:
        report = _build(organization, filters, zone, today, now)
        cache.set(key, report, CACHE_SECONDS)
    return report


def _build(organization, filters: Filters, zone, today: date, now: datetime) -> dict:
    bookings = Booking.objects.filter(organization=organization)
    if filters.location:
        bookings = bookings.filter(location_id=filters.location)
    if filters.staff:
        bookings = bookings.filter(staff_id=filters.staff)

    day_start, day_end = _midnight(zone, today), _midnight(zone, today + timedelta(days=1))
    week_start = _midnight(zone, today - timedelta(days=today.weekday()))
    week_end = week_start + timedelta(days=7)
    month_start = _midnight(zone, today.replace(day=1))
    recent_start = _midnight(zone, today - timedelta(days=filters.days - 1))
    scheduled = ~Q(status__in=INACTIVE)
    past = Q(start_datetime__gte=recent_start, start_datetime__lt=now) & ~Q(
        status=Booking.Status.REJECTED
    )
    completed = Q(status=Booking.Status.COMPLETED)

    # 1 query: every headline number.
    totals = bookings.aggregate(
        today=Count(
            "pk", filter=scheduled & Q(start_datetime__gte=day_start, start_datetime__lt=day_end)
        ),
        week=Count(
            "pk", filter=scheduled & Q(start_datetime__gte=week_start, start_datetime__lt=week_end)
        ),
        past=Count("pk", filter=past),
        cancelled=Count("pk", filter=past & Q(status=Booking.Status.CANCELLED)),
        no_show=Count("pk", filter=past & Q(status=Booking.Status.NO_SHOW)),
        revenue_month=Sum(
            "price_snapshot",
            filter=completed & Q(start_datetime__gte=month_start, start_datetime__lt=day_end),
        ),
        revenue_recent=Sum(
            "price_snapshot",
            filter=completed & Q(start_datetime__gte=recent_start, start_datetime__lt=day_end),
        ),
    )

    # 1 query: new customers in the period (customers aren't per location or provider, so the
    # narrowed version counts first-time customers of those appointments).
    customers = Customer.objects.filter(organization=organization, created_at__gte=recent_start)
    if filters.location or filters.staff:
        customers = customers.filter(bookings__in=bookings).distinct()
    new_customers = customers.exclude(status=Customer.Status.ANONYMIZED).count()

    recent = bookings.filter(
        scheduled, start_datetime__gte=recent_start, start_datetime__lt=day_end
    )
    # 1 query each: the most booked services and the busiest providers.
    popular_services = list(
        recent.values("service_id", "service__name")
        .annotate(count=Count("pk"))
        .order_by("-count", "service__name")[:TOP]
    )
    top_providers = list(
        recent.values(
            "staff_id",
            "staff__display_name",
            "staff__user__first_name",
            "staff__user__last_name",
        )
        .annotate(
            count=Count("pk"),
            completed=Count("pk", filter=completed),
            revenue=Sum("price_snapshot", filter=completed),
        )
        .order_by("-count", "staff__display_name")[:TOP]
    )
    for row in top_providers:
        full = f"{row['staff__user__first_name']} {row['staff__user__last_name']}".strip()
        row["name"] = row["staff__display_name"] or full or "Unnamed"
        row["revenue"] = row["revenue"] or Decimal(0)

    # 1 query: appointments per day for the chart.
    per_day = dict(
        recent.annotate(day=TruncDate("start_datetime", tzinfo=zone))
        .values("day")
        .annotate(count=Count("pk"))
        .values_list("day", "count")
    )
    volume = []
    for offset in range(filters.days):
        day = today - timedelta(days=filters.days - 1 - offset)
        volume.append({"day": day, "count": per_day.get(day, 0)})

    return {
        "today": totals["today"],
        "this_week": totals["week"],
        "new_customers": new_customers,
        "past_appointments": totals["past"],
        "cancellation_rate": _rate(totals["cancelled"], totals["past"]),
        "no_show_rate": _rate(totals["no_show"], totals["past"]),
        "revenue_month": totals["revenue_month"] or Decimal(0),
        "revenue_recent": totals["revenue_recent"] or Decimal(0),
        "popular_services": popular_services,
        "top_providers": top_providers,
        "volume": volume,
        "volume_max": max((item["count"] for item in volume), default=0),
    }


def upcoming_appointments(queryset, *, now: datetime | None = None, limit: int = 10):
    """Appointments that haven't finished yet and are still on, soonest first: the one in
    progress now, then the next ones. Not cached: the front desk needs them live."""
    now = now or timezone.now()
    return (
        queryset.filter(end_datetime__gt=now)
        .exclude(status__in=INACTIVE)
        .select_related("service", "staff__user", "location")
        .order_by("start_datetime")[:limit]
    )
