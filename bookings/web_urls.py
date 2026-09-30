from django.urls import path

from bookings import appointment_views, calendar_views, reception_views, waitlist_views
from bookings import web_views as views

urlpatterns = [
    path("app/reception/", reception_views.ReceptionView.as_view(), name="app-reception"),
    path(
        "app/reception/next-free/",
        reception_views.NextFreeView.as_view(),
        name="app-reception-next-free",
    ),
    path("app/waitlist/", waitlist_views.WaitlistListView.as_view(), name="app-waitlist"),
    path("app/waitlist/new/", waitlist_views.WaitlistAddView.as_view(), name="app-waitlist-new"),
    path(
        "app/waitlist/<uuid:pk>/close/",
        waitlist_views.WaitlistCloseView.as_view(),
        name="app-waitlist-close",
    ),
    path("app/calendar/", calendar_views.CalendarView.as_view(), name="app-calendar"),
    path(
        "app/calendar/events.json",
        calendar_views.CalendarEventsView.as_view(),
        name="app-calendar-events",
    ),
    path(
        "app/appointments/",
        appointment_views.AppointmentListView.as_view(),
        name="app-appointment-list",
    ),
    path(
        "app/appointments/new/",
        appointment_views.NewAppointmentView.as_view(),
        name="app-appointment-new",
    ),
    path(
        "app/appointments/walk-in/",
        appointment_views.WalkInView.as_view(),
        name="app-appointment-walk-in",
    ),
    path(
        "app/appointments/<uuid:pk>/reschedule/",
        appointment_views.RescheduleView.as_view(),
        name="app-appointment-reschedule",
    ),
    path(
        "app/appointments/<uuid:pk>/",
        calendar_views.AppointmentView.as_view(),
        name="app-appointment",
    ),
    path(
        "app/appointments/<uuid:pk>/action/",
        calendar_views.AppointmentActionView.as_view(),
        name="app-appointment-action",
    ),
    path("book/<slug:slug>/", views.LocationStepView.as_view(), name="public-booking"),
    path(
        "book/<slug:slug>/service/",
        views.ServiceStepView.as_view(),
        name="public-booking-service",
    ),
    path(
        "book/<slug:slug>/provider/",
        views.ProviderStepView.as_view(),
        name="public-booking-provider",
    ),
    path("book/<slug:slug>/time/", views.TimeStepView.as_view(), name="public-booking-time"),
    path(
        "book/<slug:slug>/details/",
        views.DetailsStepView.as_view(),
        name="public-booking-details",
    ),
    path("book/<slug:slug>/review/", views.ReviewStepView.as_view(), name="public-booking-review"),
    path(
        "book/<slug:slug>/confirmation/<uuid:public_uuid>/",
        views.ConfirmationView.as_view(),
        name="public-booking-confirmation",
    ),
    path(
        "book/<slug:slug>/waitlist/",
        views.WaitlistJoinView.as_view(),
        name="public-booking-waitlist",
    ),
    path(
        "book/<slug:slug>/waitlist/joined/",
        views.WaitlistJoinedView.as_view(),
        name="public-booking-waitlist-joined",
    ),
    path("book/<slug:slug>/theme.css", views.theme_stylesheet, name="public-booking-theme"),
]
