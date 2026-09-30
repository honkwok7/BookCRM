from django.urls import path

from saas import impersonation, views, web

urlpatterns = [
    path("saas/", views.DashboardView.as_view(), name="saas-dashboard"),
    path("saas/organizations/", views.OrganizationListView.as_view(), name="saas-organizations"),
    path(
        "saas/organizations/new/",
        views.OrganizationCreateView.as_view(),
        name="saas-organization-new",
    ),
    path(
        "saas/organizations/<uuid:pk>/",
        views.OrganizationDetailView.as_view(),
        name="saas-organization",
    ),
    path(
        "saas/organizations/<uuid:pk>/<str:action>/",
        views.OrganizationStatusView.as_view(),
        name="saas-organization-status",
    ),
    path(
        "saas/organizations/<uuid:pk>/subscription/save/",
        views.SubscriptionChangeView.as_view(),
        name="saas-subscription-change",
    ),
    path("saas/subscriptions/", views.SubscriptionListView.as_view(), name="saas-subscriptions"),
    path("saas/plans/", views.PlanListView.as_view(), name="saas-plans"),
    path("saas/plans/new/", views.PlanFormView.as_view(), name="saas-plan-new"),
    path("saas/plans/<uuid:pk>/", views.PlanFormView.as_view(), name="saas-plan"),
    path("saas/users/", views.UserListView.as_view(), name="saas-users"),
    path("saas/users/<int:pk>/", views.UserDetailView.as_view(), name="saas-user"),
    path("saas/audit/", views.AuditLogView.as_view(), name="saas-audit"),
    path(
        "saas/users/<int:pk>/impersonate/",
        views.ImpersonateView.as_view(),
        name="saas-impersonate",
    ),
    path(
        "saas/announcements/",
        views.AnnouncementListView.as_view(),
        name="saas-announcements",
    ),
    path(
        "saas/announcements/new/",
        views.AnnouncementFormView.as_view(),
        name="saas-announcement-new",
    ),
    path(
        "saas/announcements/<uuid:pk>/",
        views.AnnouncementFormView.as_view(),
        name="saas-announcement",
    ),
    path("saas/flags/", views.FlagListView.as_view(), name="saas-flags"),
    path("saas/flags/<uuid:pk>/", views.FlagDetailView.as_view(), name="saas-flag"),
    # Outside /saas/ (blocked while impersonating): the banner's End button and the
    # announcements every signed-in page loads.
    path("impersonation/end/", impersonation.end_view, name="impersonation-end"),
    path("announcements/", web.announcements, name="announcements"),
    path(
        "announcements/<uuid:pk>/dismiss/",
        web.dismiss,
        name="announcement-dismiss",
    ),
]
