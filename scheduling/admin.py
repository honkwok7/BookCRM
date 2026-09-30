"""Schedules are changed in the web app or the API (validated there): read-only here."""

from django.contrib import admin

from core.admin_mixins import ReadOnlyAdmin
from scheduling.models import (
    AvailabilityException,
    OrganizationHoliday,
    TimeOff,
    WeeklyAvailability,
)


@admin.register(WeeklyAvailability)
class WeeklyAvailabilityAdmin(ReadOnlyAdmin):
    list_display = ("organization", "staff", "day_of_week", "start_time", "end_time", "is_active")
    list_filter = ("organization", "day_of_week", "is_active")
    autocomplete_fields = ("organization", "staff")


@admin.register(AvailabilityException)
class AvailabilityExceptionAdmin(ReadOnlyAdmin):
    list_display = (
        "organization",
        "staff",
        "date",
        "unavailable_all_day",
        "start_time",
        "end_time",
    )
    list_filter = ("organization", "unavailable_all_day", "date")
    autocomplete_fields = ("organization", "staff")


@admin.register(TimeOff)
class TimeOffAdmin(ReadOnlyAdmin):
    list_display = ("organization", "staff", "start_datetime", "end_datetime", "approval_status")
    list_filter = ("organization", "approval_status")
    autocomplete_fields = ("organization", "staff")


@admin.register(OrganizationHoliday)
class OrganizationHolidayAdmin(ReadOnlyAdmin):
    list_display = ("organization", "date", "name", "full_day_closure")
    list_filter = ("organization", "full_day_closure")
    autocomplete_fields = ("organization",)
