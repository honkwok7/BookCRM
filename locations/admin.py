"""Locations are changed in the web app or the API (``locations.services``): read-only here."""

from django.contrib import admin

from core.admin_mixins import ReadOnlyAdmin
from locations.models import Location, LocationClosure, LocationHours


class LocationHoursInline(admin.TabularInline):
    model = LocationHours
    extra = 0
    fields = ("weekday", "opens_at", "closes_at")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Location)
class LocationAdmin(ReadOnlyAdmin):
    list_display = ("name", "organization", "city", "timezone", "is_default", "is_active")
    list_filter = ("is_active", "is_default", "booking_enabled")
    search_fields = ("name", "organization__name", "city")
    inlines = [LocationHoursInline]


@admin.register(LocationClosure)
class LocationClosureAdmin(ReadOnlyAdmin):
    list_display = ("location", "organization", "start_date", "end_date", "all_day", "reason")
    list_filter = ("all_day",)
    search_fields = ("location__name", "reason")
