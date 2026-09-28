from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from accounts.models import EmailVerificationToken, LoginHistory, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = (
        "id",
        "email",
        "phone_number",
        "email_verified",
        "account_status",
        "is_staff",
        "is_active",
    )
    list_filter = ("is_staff", "is_active", "is_superuser", "email_verified", "account_status")
    search_fields = ("email", "phone_number")
    ordering = ("email",)
    fieldsets = UserAdmin.fieldsets + (
        (
            "Additional Info",
            {
                "fields": (
                    "phone_number",
                    "profile_image",
                    "email_verified",
                    "account_status",
                    "terms_accepted_at",
                    "privacy_accepted_at",
                )
            },
        ),
    )


@admin.register(EmailVerificationToken)
class EmailVerificationTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "expires_at", "used_at", "created_at")
    search_fields = ("user__email", "token")
    readonly_fields = ("token", "created_at", "updated_at")


@admin.register(LoginHistory)
class LoginHistoryAdmin(admin.ModelAdmin):
    list_display = ("user", "is_successful", "ip_address", "created_at")
    list_filter = ("is_successful", "created_at")
    search_fields = ("user__email", "ip_address", "user_agent")
    readonly_fields = (
        "user",
        "is_successful",
        "ip_address",
        "user_agent",
        "created_at",
        "updated_at",
    )
