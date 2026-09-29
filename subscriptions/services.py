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
        count = Booking.objects.filter(organization=organization).count()
        if count >= plan.maximum_monthly_bookings:
            raise ValueError("Monthly booking limit reached for your subscription plan")
