from django.contrib import admin

from notifications.models import NotificationLog


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "recipient_email",
        "notification_type",
        "channel",
        "status",
        "sent_at",
    )
    list_filter = ("organization", "notification_type", "status", "channel")
    search_fields = ("recipient_email", "notification_type")
    readonly_fields = ("retry_count", "failure_reason", "sent_at", "created_at", "updated_at")
