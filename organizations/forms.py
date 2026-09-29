from django import forms
from django.contrib.auth import get_user_model

from accounts.forms import NewPasswordForm

User = get_user_model()


class InvitedSignupForm(NewPasswordForm):
    """Account details for someone accepting an invitation; the email comes from the invite."""

    field_order = ["first_name", "last_name", "new_password1", "new_password2", "accept_terms"]

    first_name = forms.CharField(
        label="First name",
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "given-name"}),
    )
    last_name = forms.CharField(
        label="Last name",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "family-name"}),
    )
    accept_terms = forms.BooleanField(
        label="I agree to the Terms of Service and the Privacy Policy.", required=True
    )

    def __init__(self, *args, invitation, **kwargs):
        super().__init__(*args, user=User(email=invitation.email), **kwargs)
        self.fields["new_password1"].label = "Password"
        self.fields["new_password2"].label = "Confirm password"
        self.fields["new_password1"].widget.attrs.pop("autofocus", None)
        self.fields["first_name"].widget.attrs["autofocus"] = True

    def clean(self):
        # The password validators compare against the person's name too.
        self.user.first_name = self.data.get("first_name", "")
        self.user.last_name = self.data.get("last_name", "")
        return super().clean()
