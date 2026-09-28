from django.urls import include, path
from rest_framework.routers import DefaultRouter

from bookings.views import BookingViewSet, CustomerViewSet, WaitlistViewSet
from scheduling.views import (
    AvailabilityExceptionViewSet,
    OrganizationHolidayViewSet,
    SlotViewSet,
    TimeOffViewSet,
    WeeklyAvailabilityViewSet,
)
from services.views import ServiceCategoryViewSet, ServiceViewSet
from staff.views import StaffProfileViewSet

router = DefaultRouter()
router.register("services", ServiceViewSet, basename="service")
router.register("service-categories", ServiceCategoryViewSet, basename="service-category")
router.register("staff", StaffProfileViewSet, basename="staff")
router.register("availability/weekly", WeeklyAvailabilityViewSet, basename="availability-weekly")
router.register(
    "availability/exceptions", AvailabilityExceptionViewSet, basename="availability-exceptions"
)
router.register("availability/time-off", TimeOffViewSet, basename="availability-time-off")
router.register(
    "availability/holidays", OrganizationHolidayViewSet, basename="availability-holidays"
)
router.register("availability/slots", SlotViewSet, basename="availability-slots")
router.register("bookings", BookingViewSet, basename="booking")
router.register("customers", CustomerViewSet, basename="customer")
router.register("waitlist", WaitlistViewSet, basename="waitlist")

urlpatterns = [
    path("", include(router.urls)),
]
