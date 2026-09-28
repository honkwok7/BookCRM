from django.contrib import admin

from organizations.models import Organization, OrganizationInvitation, OrganizationMembership


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "is_suspended", "timezone", "currency")
    list_filter = ("is_active", "is_suspended", "timezone", "currency")
    search_fields = ("name", "slug", "email")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ("organization", "user", "role", "is_active", "created_at")
    list_filter = ("role", "is_active", "organization")
    search_fields = ("organization__name", "user__email")
    autocomplete_fields = ("organization", "user")


@admin.register(OrganizationInvitation)
class OrganizationInvitationAdmin(admin.ModelAdmin):
    list_display = ("organization", "email", "role", "expires_at", "accepted_at")
    list_filter = ("role", "organization")
    search_fields = ("organization__name", "email", "token")
    readonly_fields = ("token", "accepted_at", "accepted_by")
