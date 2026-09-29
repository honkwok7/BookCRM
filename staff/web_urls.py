from django.urls import path

from staff import web_views

urlpatterns = [
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
    # Last: the tab slug would otherwise swallow "edit".
    path(
        "app/staff/<uuid:pk>/<slug:tab>/",
        web_views.StaffDetailView.as_view(),
        name="app-staff-tab",
    ),
]
