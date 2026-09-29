from django.urls import path

from locations import web_views

urlpatterns = [
    path("app/locations/", web_views.LocationListView.as_view(), name="app-location-list"),
    path("app/locations/new/", web_views.LocationCreateView.as_view(), name="app-location-new"),
    path(
        "app/locations/<uuid:pk>/",
        web_views.LocationDetailView.as_view(),
        name="app-location-detail",
    ),
    path(
        "app/locations/<uuid:pk>/edit/",
        web_views.LocationEditView.as_view(),
        name="app-location-edit",
    ),
    path(
        "app/locations/<uuid:pk>/hours/",
        web_views.LocationHoursView.as_view(),
        name="app-location-hours",
    ),
    path(
        "app/locations/<uuid:pk>/make-default/",
        web_views.LocationMakeDefaultView.as_view(),
        name="app-location-make-default",
    ),
    path(
        "app/locations/<uuid:pk>/closures/add/",
        web_views.ClosureCreateView.as_view(),
        name="app-location-closure-new",
    ),
    path(
        "app/locations/<uuid:pk>/closures/<uuid:closure_pk>/delete/",
        web_views.ClosureDeleteView.as_view(),
        name="app-location-closure-delete",
    ),
]
