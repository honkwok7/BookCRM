"""Services are changed in the web app or the API (``services.services``): read-only here."""

from django.contrib import admin

from core.admin_mixins import ReadOnlyAdmin
from services.models import Service, ServiceCategory


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(ReadOnlyAdmin):
    list_display = ("name", "organization", "slug")
    list_filter = ("organization",)
    search_fields = ("name", "slug", "organization__name")


@admin.register(Service)
class ServiceAdmin(ReadOnlyAdmin):
    list_display = (
        "name",
        "organization",
        "price",
        "currency",
        "duration_minutes",
        "is_active",
        "is_archived",
    )
    list_filter = ("organization", "is_active", "is_archived", "is_public")
    search_fields = ("name", "slug", "description", "organization__name")
