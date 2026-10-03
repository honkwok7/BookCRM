from django.urls import path

from customer_forms import web_views

urlpatterns = [
    path("app/forms/", web_views.FormListView.as_view(), name="app-form-list"),
    path("app/forms/new/", web_views.FormCreateView.as_view(), name="app-form-new"),
    path("app/forms/<uuid:pk>/", web_views.FormBuilderView.as_view(), name="app-form-builder"),
    path(
        "app/forms/<uuid:pk>/settings/",
        web_views.FormSettingsView.as_view(),
        name="app-form-settings",
    ),
    path("app/forms/<uuid:pk>/delete/", web_views.FormDeleteView.as_view(), name="app-form-delete"),
    path(
        "app/forms/<uuid:pk>/publish/", web_views.FormPublishView.as_view(), name="app-form-publish"
    ),
    path(
        "app/forms/<uuid:pk>/discard/", web_views.FormDiscardView.as_view(), name="app-form-discard"
    ),
    path(
        "app/forms/<uuid:pk>/questions/new/",
        web_views.QuestionCreateView.as_view(),
        name="app-form-question-new",
    ),
    path(
        "app/forms/questions/<uuid:pk>/edit/",
        web_views.QuestionEditView.as_view(),
        name="app-form-question-edit",
    ),
    path(
        "app/forms/questions/<uuid:pk>/delete/",
        web_views.QuestionDeleteView.as_view(),
        name="app-form-question-delete",
    ),
    path(
        "app/forms/questions/<uuid:pk>/move/",
        web_views.QuestionMoveView.as_view(),
        name="app-form-question-move",
    ),
]
