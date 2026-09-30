"""Admin building blocks."""

from django.contrib import admin


class ReadOnlyAdmin(admin.ModelAdmin):
    """Look, don't edit: the model changes only through its service layer (validation, plan
    limits, locking, audit, derived data such as the service/staff mirror)."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
