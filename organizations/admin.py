"""Django admin for organizations: look, don't edit.

Organizations, memberships and invitations change through the audited services (sign-up,
invitations, the team screens). Here they are read-only, except suspending and reactivating an
organization, which the admin actions do through ``suspend_organization`` and
``reactivate_organization`` so the change is audited.
"""

from django.contrib import admin, messages

from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from organizations.tenancy import reactivate_organization, suspend_organization


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Organization)
class OrganizationAdmin(ReadOnlyAdmin):
    list_display = ("name", "slug", "is_active", "is_suspended", "timezone", "currency")
    list_filter = ("is_active", "is_suspended", "timezone", "currency")
    search_fields = ("name", "slug", "email")
    actions = ("suspend", "reactivate")

    @admin.action(description="Suspend the selected organizations")
    def suspend(self, request, queryset):
        count = 0
        for organization in queryset.filter(is_suspended=False):
            suspend_organization(
                organization=organization,
                reason="Suspended by a platform admin",
                actor=request.user,
            )
            count += 1
        self.message_user(request, f"Suspended {count} organization(s).", messages.SUCCESS)

    @admin.action(description="Reactivate the selected organizations")
    def reactivate(self, request, queryset):
        count = 0
        for organization in queryset.filter(is_suspended=True):
            reactivate_organization(organization=organization, actor=request.user)
            count += 1
        self.message_user(request, f"Reactivated {count} organization(s).", messages.SUCCESS)

    def get_actions(self, request):
        # Actions need no change permission; only platform admins (superusers) may run them.
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("suspend", None)
            actions.pop("reactivate", None)
        return actions


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(ReadOnlyAdmin):
    list_display = ("organization", "user", "role", "is_active", "created_at")
    list_filter = ("role", "is_active", "organization")
    search_fields = ("organization__name", "user__email")


@admin.register(OrganizationInvitation)
class OrganizationInvitationAdmin(ReadOnlyAdmin):
    list_display = ("organization", "email", "role", "expires_at", "accepted_at")
    list_filter = ("role", "organization")
    search_fields = ("organization__name", "email")
    exclude = ("token",)  # a usable token is a way in: never shown
