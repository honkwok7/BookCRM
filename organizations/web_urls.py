from django.urls import path

from organizations import web_views

urlpatterns = [
    path(
        "accept-invitation/",
        web_views.AcceptInvitationView.as_view(),
        name="accept-invitation",
    ),
    path(
        "app/switch/<slug:slug>/",
        web_views.SwitchOrganizationView.as_view(),
        name="switch-organization",
    ),
    path("app/team/", web_views.TeamView.as_view(), name="app-team"),
]
