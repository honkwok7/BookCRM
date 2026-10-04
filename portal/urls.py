from django.urls import path

from portal import views

urlpatterns = [
    path("portal/", views.PortalHomeView.as_view(), name="portal-home"),
    path("portal/<slug:slug>/", views.PortalOrganizationView.as_view(), name="portal-organization"),
    path(
        "portal/<slug:slug>/appointments/",
        views.PortalAppointmentsView.as_view(),
        name="portal-appointments",
    ),
    path(
        "portal/<slug:slug>/appointments/<uuid:pk>/",
        views.PortalAppointmentView.as_view(),
        name="portal-appointment",
    ),
    path(
        "portal/<slug:slug>/appointments/<uuid:pk>/cancel/",
        views.PortalCancelView.as_view(),
        name="portal-cancel",
    ),
    path(
        "portal/<slug:slug>/appointments/<uuid:pk>/reschedule/",
        views.PortalRescheduleView.as_view(),
        name="portal-reschedule",
    ),
    path("portal/<slug:slug>/profile/", views.PortalProfileView.as_view(), name="portal-profile"),
    path("portal/<slug:slug>/forms/", views.PortalFormsView.as_view(), name="portal-forms"),
    path("portal/<slug:slug>/forms/<uuid:pk>/", views.PortalFormView.as_view(), name="portal-form"),
]
