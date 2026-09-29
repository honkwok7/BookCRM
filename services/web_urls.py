from django.urls import path

from services import web_views

urlpatterns = [
    path("app/services/", web_views.ServiceListView.as_view(), name="app-service-list"),
    path("app/services/new/", web_views.ServiceCreateView.as_view(), name="app-service-new"),
    path(
        "app/services/<uuid:pk>/edit/",
        web_views.ServiceEditView.as_view(),
        name="app-service-edit",
    ),
    path(
        "app/services/categories/new/",
        web_views.CategoryCreateView.as_view(),
        name="app-service-category-new",
    ),
    path(
        "app/services/categories/<uuid:pk>/edit/",
        web_views.CategoryEditView.as_view(),
        name="app-service-category-edit",
    ),
    path(
        "app/services/categories/<uuid:pk>/delete/",
        web_views.CategoryDeleteView.as_view(),
        name="app-service-category-delete",
    ),
]
