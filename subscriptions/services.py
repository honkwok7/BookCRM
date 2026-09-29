from zoneinfo import ZoneInfo

from django.utils import timezone

from bookings.models import Booking
from locations.models import Location
from services.models import Service
from staff.models import StaffProfile


def enforce_plan_limit(organization, limit_type: str):
    subscription = getattr(organization, "subscription", None)
    if subscription is None:
        return
    plan = subscription.plan

    if limit_type == "staff":
        count = StaffProfile.objects.filter(organization=organization, is_active=True).count()
        if count >= plan.maximum_staff:
            raise ValueError("Staff limit reached for your subscription plan")

    if limit_type == "services":
        count = Service.objects.filter(organization=organization, is_archived=False).count()
        if count >= plan.maximum_services:
            raise ValueError("Service limit reached for your subscription plan")

    if limit_type == "locations":
        count = Location.objects.filter(organization=organization, is_active=True).count()
        if count >= plan.maximum_locations:
            raise ValueError("Location limit reached for your subscription plan")

    if limit_type == "bookings":
        # Bookings made this calendar month (the organization's time zone), whatever their
        # status: a cancelled booking still used the allowance.
        now = timezone.now().astimezone(ZoneInfo(organization.timezone))
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        count = Booking.objects.filter(
            organization=organization, created_at__gte=month_start
        ).count()
        if count >= plan.maximum_monthly_bookings:
            raise ValueError("Monthly booking limit reached for your subscription plan")
