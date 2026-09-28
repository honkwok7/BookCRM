from django.contrib import admin

from bookings.models import (
    Booking,
    BookingActivityLog,
    BookingStatusHistory,
    Customer,
    WaitlistEntry,
)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "organization", "total_bookings", "last_appointment")
    list_filter = ("organization",)
    search_fields = ("name", "email", "phone")


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "organization",
        "customer_name",
        "service",
        "staff",
        "start_datetime",
        "status",
    )
    list_filter = ("organization", "status", "payment_status")
    search_fields = ("reference", "customer_name", "customer_email")
    readonly_fields = ("reference", "public_uuid", "created_at", "updated_at")
    autocomplete_fields = (
        "organization",
        "customer",
        "service",
        "staff",
        "cancelled_by",
        "rescheduled_from",
    )


@admin.register(BookingStatusHistory)
class BookingStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ("booking", "old_status", "new_status", "changed_by", "created_at")
    list_filter = ("new_status",)
    autocomplete_fields = ("booking", "changed_by")


@admin.register(BookingActivityLog)
class BookingActivityLogAdmin(admin.ModelAdmin):
    list_display = ("booking", "organization", "actor", "action", "created_at")
    list_filter = ("organization", "action")
    autocomplete_fields = ("booking", "organization", "actor")


@admin.register(WaitlistEntry)
class WaitlistEntryAdmin(admin.ModelAdmin):
    list_display = ("organization", "service", "customer_email", "status", "created_at")
    list_filter = ("organization", "status")
    search_fields = ("customer_email", "customer_name")
