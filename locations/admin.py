from django.contrib import admin

from locations.models import Location, LocationClosure, LocationHours


class LocationHoursInline(admin.TabularInline):
    model = LocationHours
    extra = 0
    fields = ("weekday", "opens_at", "closes_at")


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "city", "timezone", "is_default", "is_active")
    list_filter = ("is_active", "is_default", "booking_enabled")
    search_fields = ("name", "organization__name", "city")
    readonly_fields = ("slug", "is_default")
    inlines = [LocationHoursInline]


@admin.register(LocationClosure)
class LocationClosureAdmin(admin.ModelAdmin):
    list_display = ("location", "organization", "start_date", "end_date", "all_day", "reason")
    list_filter = ("all_day",)
    search_fields = ("location__name", "reason")
