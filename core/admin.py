from django.contrib import admin

from core.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "organization",
        "user",
        "actor_type",
        "action",
        "object_type",
        "object_identifier",
    )
    list_filter = ("action", "object_type", "created_at")
    search_fields = ("action", "object_type", "object_identifier", "user__email")
    readonly_fields = (
        "id",
        "organization",
        "user",
        "actor_type",
        "impersonator",
        "action",
        "object_type",
        "object_identifier",
        "metadata",
        "ip_address",
        "user_agent",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
