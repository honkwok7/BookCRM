from django.urls import path

from organizations.views import (
    CurrentOrganizationView,
    InvitationAcceptView,
    InvitationCreateView,
    OrganizationMembershipListView,
)

urlpatterns = [
    path("organizations/current/", CurrentOrganizationView.as_view(), name="organization-current"),
    path(
        "organizations/memberships/",
        OrganizationMembershipListView.as_view(),
        name="organization-memberships",
    ),
    path(
        "organizations/invitations/",
        InvitationCreateView.as_view(),
        name="organization-invitations-create",
    ),
    path(
        "organizations/invitations/accept/",
        InvitationAcceptView.as_view(),
        name="organization-invitations-accept",
    ),
]
