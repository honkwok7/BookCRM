from django.db.models import Sum
from django.utils import timezone

from bookings.models import Booking, Customer
from services.models import Service
from staff.models import StaffProfile


def organization_dashboard_summary(organization):
    now = timezone.now()
    today = now.date()
    this_week_start = today - timezone.timedelta(days=today.weekday())
    this_month_start = today.replace(day=1)

    bookings_qs = Booking.objects.filter(organization=organization)
    today_qs = bookings_qs.filter(start_datetime__date=today)

    return {
        "appointments_today": today_qs.count(),
        "appointments_this_week": bookings_qs.filter(
            start_datetime__date__gte=this_week_start
        ).count(),
        "appointments_this_month": bookings_qs.filter(
            start_datetime__date__gte=this_month_start
        ).count(),
        "confirmed_bookings": bookings_qs.filter(status=Booking.Status.CONFIRMED).count(),
        "completed_bookings": bookings_qs.filter(status=Booking.Status.COMPLETED).count(),
        "cancelled_bookings": bookings_qs.filter(status=Booking.Status.CANCELLED).count(),
        "estimated_revenue": float(
            bookings_qs.filter(status=Booking.Status.COMPLETED)
            .aggregate(total=Sum("price_snapshot"))
            .get("total")
            or 0
        ),
        "active_customers": Customer.objects.filter(organization=organization).count(),
        "active_staff": StaffProfile.objects.filter(
            organization=organization, is_active=True
        ).count(),
        "active_services": Service.objects.filter(
            organization=organization, is_archived=False
        ).count(),
    }
