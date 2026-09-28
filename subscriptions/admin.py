from django.contrib import admin

from subscriptions.models import Plan, Subscription


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "monthly_price",
        "yearly_price",
        "maximum_staff",
        "maximum_services",
        "is_active",
    )
    list_filter = ("is_active", "analytics_enabled", "api_access_enabled")
    search_fields = ("name", "slug")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "plan",
        "status",
        "billing_cycle",
        "current_period_end",
        "cancel_at_period_end",
    )
    list_filter = ("status", "billing_cycle")
    search_fields = ("organization__name", "external_customer_id", "external_subscription_id")
    autocomplete_fields = ("organization", "plan")
