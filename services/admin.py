from django.contrib import admin

from services.models import Service, ServiceCategory


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "slug")
    list_filter = ("organization",)
    search_fields = ("name", "slug", "organization__name")


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
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
    autocomplete_fields = ("organization", "category", "assigned_staff_members")
