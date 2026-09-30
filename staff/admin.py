"""Staff are changed in the web app or the API (``staff.services``): read-only here."""

from django.contrib import admin

from core.admin_mixins import ReadOnlyAdmin
from staff.models import StaffProfile


@admin.register(StaffProfile)
class StaffProfileAdmin(ReadOnlyAdmin):
    list_display = ("user", "organization", "job_title", "is_active", "is_accepting_bookings")
    list_filter = ("organization", "is_active", "is_accepting_bookings")
    search_fields = ("user__email", "job_title", "bio")
