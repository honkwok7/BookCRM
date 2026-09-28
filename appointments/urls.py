from django.urls import path

from appointments.views import (
    AppointmentCancelView,
    AppointmentCompleteView,
    AppointmentConfirmView,
    AppointmentListCreateView,
    AppointmentNoShowView,
    AppointmentRescheduleView,
    MyAppointmentListView,
)

urlpatterns = [
    path("appointments/", AppointmentListCreateView.as_view(), name="appointment-list-create"),
    path("my-appointments/", MyAppointmentListView.as_view(), name="my-appointments"),
    path(
        "appointments/<int:pk>/cancel/", AppointmentCancelView.as_view(), name="appointment-cancel"
    ),
    path(
        "appointments/<int:pk>/confirm/",
        AppointmentConfirmView.as_view(),
        name="appointment-confirm",
    ),
    path(
        "appointments/<int:pk>/complete/",
        AppointmentCompleteView.as_view(),
        name="appointment-complete",
    ),
    path(
        "appointments/<int:pk>/no-show/",
        AppointmentNoShowView.as_view(),
        name="appointment-no-show",
    ),
    path(
        "appointments/<int:pk>/reschedule/",
        AppointmentRescheduleView.as_view(),
        name="appointment-reschedule",
    ),
]
