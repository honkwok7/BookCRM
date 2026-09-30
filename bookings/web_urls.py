from django.urls import path

from bookings import calendar_views
from bookings import web_views as views

urlpatterns = [
    path("app/calendar/", calendar_views.CalendarView.as_view(), name="app-calendar"),
    path(
        "app/calendar/events.json",
        calendar_views.CalendarEventsView.as_view(),
        name="app-calendar-events",
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
    path("book/<slug:slug>/theme.css", views.theme_stylesheet, name="public-booking-theme"),
]
