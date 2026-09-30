"""Platform forms (M5.5). They collect input; saas.services applies the rules and audits."""

from __future__ import annotations

from django import forms

from core.validators import validate_timezone
from saas.services import PLAN_FIELDS
from subscriptions.models import Plan, Subscription


class OrganizationCreateForm(forms.Form):
    name = forms.CharField(max_length=255)
    slug = forms.SlugField(
        max_length=60,
        required=False,
        label="Address",
        help_text="Used in /book/<address>/. Left empty: made from the name.",
    )
    owner_email = forms.EmailField(
        label="Owner's email", help_text="They get an invitation to join as the owner."
    )
    plan = forms.ModelChoiceField(
        queryset=Plan.objects.filter(is_active=True).order_by("monthly_price")
    )
    timezone_name = forms.CharField(
        max_length=64, initial="UTC", label="Time zone", validators=[validate_timezone]
    )
    currency = forms.CharField(max_length=10, initial="USD")
    trial_days = forms.IntegerField(
        min_value=0, max_value=90, initial=14, help_text="0 starts the subscription as active."
    )


class ReasonForm(forms.Form):
    reason = forms.CharField(max_length=255, widget=forms.Textarea(attrs={"rows": 2}))


class SubscriptionForm(forms.ModelForm):
    class Meta:
        model = Subscription
        fields = ("plan", "status", "billing_cycle", "trial_end", "current_period_end")
        widgets = {
            "trial_end": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "current_period_end": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }


class PlanForm(forms.ModelForm):
    class Meta:
        model = Plan
        fields = PLAN_FIELDS


class UserSearchForm(forms.Form):
    q = forms.CharField(required=False)
