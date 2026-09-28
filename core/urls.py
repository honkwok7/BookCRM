from django.urls import path

from core.views import healthcheck_view, readiness_view

urlpatterns = [
    path("health/", healthcheck_view, name="health"),
    path("ready/", readiness_view, name="ready"),
]
