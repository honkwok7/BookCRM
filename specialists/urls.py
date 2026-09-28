from django.urls import path

from specialists.views import (
    SpecialistAppointmentsView,
    SpecialistAvailableSlotsView,
    SpecialistDetailView,
    SpecialistListView,
    SpecialistWorkingHourDeleteView,
    SpecialistWorkingHoursView,
)

urlpatterns = [
    path("specialists/", SpecialistListView.as_view(), name="specialist-list"),
    path("specialists/<int:pk>/", SpecialistDetailView.as_view(), name="specialist-detail"),
    path(
        "specialists/<int:pk>/working-hours/",
        SpecialistWorkingHoursView.as_view(),
        name="specialist-working-hours",
    ),
    path(
        "specialists/<int:pk>/working-hours/<int:working_hour_id>/",
        SpecialistWorkingHourDeleteView.as_view(),
        name="specialist-working-hour-detail",
    ),
    path(
        "specialists/<int:pk>/available-slots/",
        SpecialistAvailableSlotsView.as_view(),
        name="specialist-available-slots",
    ),
    path(
        "specialists/<int:pk>/appointments/",
        SpecialistAppointmentsView.as_view(),
        name="specialist-appointments",
    ),
]
