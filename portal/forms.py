"""Portal forms (M5.4). They only collect input; crm.services and bookings.services apply the
rules (consent stamping, auditing, cancellation deadline, availability)."""

from __future__ import annotations

from django import forms

from bookings.models import Customer


class ProfileForm(forms.ModelForm):
    """What a customer may change about themselves at one business. The email stays the
    account's: it is how the portal finds their appointments."""

    class Meta:
        model = Customer
        fields = (
            "first_name",
            "last_name",
            "phone",
            "email_consent",
            "sms_consent",
            "marketing_consent",
        )
        labels = {
            "email_consent": "Email me about my appointments",
            "sms_consent": "Text me about my appointments",
            "marketing_consent": "Send me news and offers",
        }

    def clean(self):
        data = super().clean()
        if not (data.get("first_name") or data.get("last_name")):
            self.add_error("first_name", "Please give your name.")
        return data


class CancelForm(forms.Form):
    reason = forms.CharField(
        required=False,
        max_length=255,
        label="Reason",
        help_text="Optional. It helps the team.",
    )
