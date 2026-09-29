from django.urls import path

from crm import web_views

urlpatterns = [
    path("app/search/", web_views.SearchView.as_view(), name="app-search"),
    path("app/customers/", web_views.CustomerListView.as_view(), name="crm-customer-list"),
    path("app/customers/new/", web_views.CustomerCreateView.as_view(), name="crm-customer-new"),
    path(
        "app/customers/<uuid:pk>/",
        web_views.CustomerDetailView.as_view(),
        name="crm-customer-detail",
    ),
    path(
        "app/customers/<uuid:pk>/edit/",
        web_views.CustomerEditView.as_view(),
        name="crm-customer-edit",
    ),
    path(
        "app/customers/<uuid:pk>/tags/",
        web_views.CustomerTagsView.as_view(),
        name="crm-customer-tags",
    ),
    path(
        "app/customers/<uuid:pk>/notes/add/",
        web_views.NoteCreateView.as_view(),
        name="crm-note-create",
    ),
    path(
        "app/customers/<uuid:pk>/notes/<uuid:note_pk>/edit/",
        web_views.NoteEditView.as_view(),
        name="crm-note-edit",
    ),
    path(
        "app/customers/<uuid:pk>/notes/<uuid:note_pk>/delete/",
        web_views.NoteDeleteView.as_view(),
        name="crm-note-delete",
    ),
    path(
        "app/customers/<uuid:pk>/notes/<uuid:note_pk>/pin/",
        web_views.NotePinView.as_view(),
        name="crm-note-pin",
    ),
    # Last: the tab slug would otherwise swallow "edit" and "tags".
    path(
        "app/customers/<uuid:pk>/<slug:tab>/",
        web_views.CustomerDetailView.as_view(),
        name="crm-customer-tab",
    ),
]
