from django.urls import include, path
from rest_framework.routers import DefaultRouter

from api.search import GlobalSearchView
from bookings.views import BookingViewSet, WaitlistViewSet
from core.audit_api import AuditLogViewSet
from crm.views import CustomerNoteViewSet, CustomerViewSet, TagViewSet
from customer_forms.views import FormQuestionViewSet, FormTemplateViewSet
from locations.views import LocationClosureViewSet, LocationViewSet
from scheduling.views import (
    AvailabilityExceptionViewSet,
    OrganizationHolidayViewSet,
    SlotViewSet,
    TimeOffViewSet,
    WeeklyAvailabilityViewSet,
)
from services.views import ServiceCategoryViewSet, ServiceViewSet
from staff.views import StaffProfileViewSet, StaffServiceOfferingViewSet

router = DefaultRouter()
router.register("services", ServiceViewSet, basename="service")
router.register("service-categories", ServiceCategoryViewSet, basename="service-category")
router.register("staff", StaffProfileViewSet, basename="staff")
router.register("staff-offerings", StaffServiceOfferingViewSet, basename="staff-offering")
router.register("locations", LocationViewSet, basename="location")
router.register("location-closures", LocationClosureViewSet, basename="location-closure")
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
router.register("tags", TagViewSet, basename="tag")
router.register("customer-notes", CustomerNoteViewSet, basename="customer-note")
router.register("waitlist", WaitlistViewSet, basename="waitlist")
router.register("audit-logs", AuditLogViewSet, basename="audit-log")
router.register("forms", FormTemplateViewSet, basename="form")
router.register("form-questions", FormQuestionViewSet, basename="form-question")

urlpatterns = [
    path("search/", GlobalSearchView.as_view(), name="global-search"),
    path("", include(router.urls)),
]
