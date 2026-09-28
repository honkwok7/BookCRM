from django.db import models

from core.models import BaseUUIDModel


class Plan(BaseUUIDModel):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, unique=True)
    monthly_price = models.DecimalField(max_digits=12, decimal_places=2)
    yearly_price = models.DecimalField(max_digits=12, decimal_places=2)
    maximum_staff = models.PositiveIntegerField(default=1)
    maximum_services = models.PositiveIntegerField(default=3)
    maximum_monthly_bookings = models.PositiveIntegerField(default=100)
    analytics_enabled = models.BooleanField(default=False)
    api_access_enabled = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["monthly_price"]

    def __str__(self):
        return self.name


class Subscription(BaseUUIDModel):
    class Status(models.TextChoices):
        TRIALING = "trialing", "Trialing"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past Due"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    class BillingCycle(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"

    organization = models.OneToOneField(
        "organizations.Organization", on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TRIALING)
    billing_cycle = models.CharField(
        max_length=20, choices=BillingCycle.choices, default=BillingCycle.MONTHLY
    )
    trial_start = models.DateTimeField(null=True, blank=True)
    trial_end = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    external_customer_id = models.CharField(max_length=255, blank=True)
    external_subscription_id = models.CharField(max_length=255, blank=True)
