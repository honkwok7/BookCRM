from django.contrib import admin

from staff.models import StaffProfile


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "job_title", "is_active", "is_accepting_bookings")
    list_filter = ("organization", "is_active", "is_accepting_bookings")
    search_fields = ("user__email", "job_title", "bio")
    autocomplete_fields = ("user", "organization")
