"""The platform dashboard's numbers (M5.5). Counts and sums only: no organization's records.

MRR is monthly recurring revenue from the plan prices of active subscriptions (a yearly one
counts as a twelfth of its price). There is no payment provider yet, so this is what the plans
say, not what was collected.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Case, Count, DecimalField, F, Q, Sum, When
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from bookings.models import Booking
from notifications.models import NotificationLog
from organizations.models import Organization
from saas.tasks import HEARTBEAT_KEY
from subscriptions.models import Subscription

CACHE_KEY = "saas:overview"
CACHE_SECONDS = 60
RECENT_DAYS = 30
HEARTBEAT_STALE = timedelta(minutes=5)

User = get_user_model()
Status = Subscription.Status


def monthly_value():
    """A subscription's monthly value (a yearly plan counts as a twelfth of its price)."""
    return Case(
        When(billing_cycle=Subscription.BillingCycle.YEARLY, then=F("plan__yearly_price") / 12),
        default=F("plan__monthly_price"),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


def platform_overview(*, now: datetime | None = None, use_cache: bool = True) -> dict:
    if use_cache:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached
    now = now or timezone.now()
    since = now - timedelta(days=RECENT_DAYS)

    organizations = Organization.objects.aggregate(
        total=Count("pk"),
        active=Count("pk", filter=Q(is_active=True, is_suspended=False)),
        suspended=Count("pk", filter=Q(is_suspended=True)),
        new=Count("pk", filter=Q(created_at__gte=since)),
    )
    live = Subscription.objects.filter(organization__is_active=True)
    subscriptions = live.aggregate(
        trialing=Count("pk", filter=Q(status=Status.TRIALING)),
        paying=Count("pk", filter=Q(status=Status.ACTIVE, plan__monthly_price__gt=0)),
        past_due=Count("pk", filter=Q(status=Status.PAST_DUE)),
        trials_ending=Count(
            "pk",
            filter=Q(
                status=Status.TRIALING, trial_end__gte=now, trial_end__lt=now + timedelta(days=7)
            ),
        ),
        mrr=Sum(monthly_value(), filter=Q(status=Status.ACTIVE)),
    )
    plans = list(
        live.filter(status__in=(Status.TRIALING, Status.ACTIVE, Status.PAST_DUE))
        .values("plan__name")
        .annotate(count=Count("pk"))
        .order_by("-count", "plan__name")
    )
    users = User.objects.aggregate(
        total=Count("pk", filter=Q(is_active=True)),
        active=Count("pk", filter=Q(is_active=True, last_login__gte=since)),
        new=Count("pk", filter=Q(date_joined__gte=since)),
    )
    appointments = Booking.objects.aggregate(
        created=Count("pk", filter=Q(created_at__gte=since)),
        week=Count("pk", filter=Q(created_at__gte=now - timedelta(days=7))),
    )
    failed = NotificationLog.objects.filter(
        status=NotificationLog.Status.FAILED, created_at__gte=now - timedelta(days=1)
    ).count()

    beat = parse_datetime(cache.get(HEARTBEAT_KEY) or "")
    alerts = []
    if beat is None:
        alerts.append(("warning", "No background worker heartbeat yet (celery beat and worker)."))
    elif now - beat > HEARTBEAT_STALE:
        alerts.append(("error", f"The background worker's last heartbeat was at {beat:%H:%M} UTC."))
    if failed:
        alerts.append(("error", f"{failed} notification{'s' if failed != 1 else ''} failed today."))
    if subscriptions["past_due"]:
        alerts.append(("warning", f"{subscriptions['past_due']} subscription(s) past due."))

    overview = {
        "organizations": organizations,
        "subscriptions": subscriptions,
        "mrr": subscriptions["mrr"] or Decimal("0"),
        "plans": plans,
        "users": users,
        "appointments": appointments,
        "failed_notifications": failed,
        "heartbeat": beat,
        "alerts": alerts,
        "recent_days": RECENT_DAYS,
        "computed_at": now,
    }
    cache.set(CACHE_KEY, overview, CACHE_SECONDS)
    return overview
