from django.contrib import admin

from bookings.models import (
    Booking,
    BookingActivityLog,
    BookingStatusHistory,
    Customer,
    WaitlistEntry,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Inspection only: these records change through services (bookings.services,
    crm.services), which enforce tenant consistency, locking, lifecycle rules and auditing.
    Editing them here would bypass all of that."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Customer)
class CustomerAdmin(ReadOnlyAdmin):
    list_display = ("name", "email", "phone", "organization", "status")
    list_filter = ("organization", "status")
    search_fields = ("name", "preferred_name", "email", "phone")


@admin.register(Booking)
class BookingAdmin(ReadOnlyAdmin):
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


@admin.register(BookingStatusHistory)
class BookingStatusHistoryAdmin(ReadOnlyAdmin):
    list_display = ("booking", "old_status", "new_status", "changed_by", "created_at")
    list_filter = ("new_status",)


@admin.register(BookingActivityLog)
class BookingActivityLogAdmin(ReadOnlyAdmin):
    list_display = ("booking", "organization", "actor", "action", "created_at")
    list_filter = ("organization", "action")


@admin.register(WaitlistEntry)
class WaitlistEntryAdmin(ReadOnlyAdmin):
    list_display = ("organization", "service", "customer_email", "status", "created_at")
    list_filter = ("organization", "status")
    search_fields = ("customer_email", "customer_name")
