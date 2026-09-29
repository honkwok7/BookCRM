from datetime import datetime, time, timedelta

from django.db.models import Sum
from django.utils import timezone

from bookings.models import Booking, Customer
from core.web import organization_zone
from services.models import Service
from staff.models import StaffProfile


def _local_midnight(zone, day) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone)


def organization_dashboard_summary(organization):
    """Headline numbers. "Today", "this week" (from Monday) and "this month" are calendar
    periods in the organization's time zone, and count appointments that are still on
    (cancelled and rejected ones are reported separately)."""
    zone = organization_zone(organization)
    today = timezone.now().astimezone(zone).date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)

    bookings_qs = Booking.objects.filter(organization=organization)
    scheduled = bookings_qs.exclude(status__in=(Booking.Status.CANCELLED, Booking.Status.REJECTED))

    def between(start_day, end_day):
        return scheduled.filter(
            start_datetime__gte=_local_midnight(zone, start_day),
            start_datetime__lt=_local_midnight(zone, end_day),
        ).count()

    return {
        "appointments_today": between(today, today + timedelta(days=1)),
        "appointments_this_week": between(week_start, week_start + timedelta(days=7)),
        "appointments_this_month": between(month_start, next_month),
        "confirmed_bookings": bookings_qs.filter(status=Booking.Status.CONFIRMED).count(),
        "completed_bookings": bookings_qs.filter(status=Booking.Status.COMPLETED).count(),
        "cancelled_bookings": bookings_qs.filter(status=Booking.Status.CANCELLED).count(),
        "estimated_revenue": float(
            bookings_qs.filter(status=Booking.Status.COMPLETED)
            .aggregate(total=Sum("price_snapshot"))
            .get("total")
            or 0
        ),
        "active_customers": Customer.objects.filter(
            organization=organization, status=Customer.Status.ACTIVE
        ).count(),
        "active_staff": StaffProfile.objects.filter(
            organization=organization, is_active=True
        ).count(),
        "active_services": Service.objects.filter(
            organization=organization, is_archived=False
        ).count(),
    }
