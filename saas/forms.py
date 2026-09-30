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


# -- M5.5b --------------------------------------------------------------------------------------


class AnnouncementForm(forms.ModelForm):
    class Meta:
        from saas.models import Announcement

        model = Announcement
        fields = ("title", "body", "audience", "level", "starts_at", "ends_at", "is_active")
        widgets = {
            "body": forms.Textarea(attrs={"rows": 3}),
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }
        labels = {"is_active": "Show it"}


class FlagForm(forms.Form):
    key = forms.SlugField(
        max_length=80, help_text='Used in code: is_enabled("<key>", organization).'
    )
    description = forms.CharField(max_length=255, required=False)
    enabled = forms.BooleanField(required=False, label="On for everyone")


class OverrideForm(forms.Form):
    organization = forms.SlugField(label="Organization address")
    state = forms.ChoiceField(
        choices=(("on", "On"), ("off", "Off"), ("default", "Remove the override"))
    )


class ImpersonateForm(forms.Form):
    organization = forms.ModelChoiceField(queryset=None, empty_label=None)
    reason = forms.CharField(
        max_length=255, help_text="Kept with the session, e.g. the support ticket."
    )
    minutes = forms.TypedChoiceField(
        coerce=int, choices=[(15, "15 minutes"), (30, "30 minutes"), (60, "1 hour")], initial=30
    )
    password = forms.CharField(
        label="Your password", widget=forms.PasswordInput, help_text="Confirm it's you."
    )

    def __init__(self, *args, organizations, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["organization"].queryset = organizations
