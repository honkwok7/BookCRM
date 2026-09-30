from django.urls import path

from bookings.calendar_views import StaffCalendarView
from crm.web_views import ProviderCustomerListView
from staff import provider_views, web_views

urlpatterns = [
    # The provider area (M5.3): the signed-in member's own work.
    path(
        "staff/dashboard/",
        provider_views.ProviderDashboardView.as_view(),
        name="staff-dashboard",
    ),
    path("staff/calendar/", StaffCalendarView.as_view(), name="staff-calendar"),
    path("staff/customers/", ProviderCustomerListView.as_view(), name="staff-customers"),
    path(
        "staff/availability/",
        provider_views.ProviderAvailabilityView.as_view(),
        name="staff-availability",
    ),
    path(
        "staff/time-off/<uuid:pk>/cancel/",
        provider_views.ProviderTimeOffCancelView.as_view(),
        name="staff-time-off-cancel",
    ),
    path("app/staff/", web_views.StaffListView.as_view(), name="app-staff-list"),
    path("app/staff/new/", web_views.StaffCreateView.as_view(), name="app-staff-new"),
    path("app/staff/<uuid:pk>/", web_views.StaffDetailView.as_view(), name="app-staff-detail"),
    path("app/staff/<uuid:pk>/edit/", web_views.StaffEditView.as_view(), name="app-staff-edit"),
    path(
        "app/staff/<uuid:pk>/services/edit/",
        web_views.StaffServicesView.as_view(),
        name="app-staff-services",
    ),
    path(
        "app/staff/<uuid:pk>/locations/edit/",
        web_views.StaffLocationsView.as_view(),
        name="app-staff-locations",
    ),
    path(
        "app/staff/<uuid:pk>/time-off/<uuid:entry_pk>/decide/",
        web_views.StaffTimeOffDecideView.as_view(),
        name="app-staff-time-off-decide",
    ),
    # Last: the tab slug would otherwise swallow "edit".
    path(
        "app/staff/<uuid:pk>/<slug:tab>/",
        web_views.StaffDetailView.as_view(),
        name="app-staff-tab",
    ),
]
