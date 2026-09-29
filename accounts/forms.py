from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import (
    password_validators_help_text_html,
    validate_password,
)

User = get_user_model()


class EmailAuthenticationForm(AuthenticationForm):
    """Sign in by email (``User.USERNAME_FIELD``); the email is matched ignoring case."""

    username = forms.EmailField(
        label="Email",
        max_length=254,
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}),
    )
    error_messages = {
        "invalid_login": "That email and password don't match an account. Please try again.",
        "inactive": "This account is inactive.",
    }


class PasswordResetRequestForm(forms.Form):
    email = forms.EmailField(
        label="Email",
        max_length=254,
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}),
    )


class NewPasswordForm(forms.Form):
    """Choose a password, checked against the configured password validators."""

    new_password1 = forms.CharField(
        label="New password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "autofocus": True}),
        help_text=password_validators_help_text_html(),
    )
    new_password2 = forms.CharField(
        label="Confirm new password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        first, second = cleaned.get("new_password1"), cleaned.get("new_password2")
        if first and second and first != second:
            self.add_error("new_password2", "The two passwords don't match.")
        elif first:
            try:
                validate_password(first, user=self.user)
            except forms.ValidationError as error:
                self.add_error("new_password1", error)
        return cleaned
