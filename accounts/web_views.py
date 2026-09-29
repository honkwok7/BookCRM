"""Sign-in, sign-out, password reset and email verification pages.

The emailed links (accounts/tasks.py) point at ``/reset-password/`` and ``/verify-email/``.
Password-reset emails are sent by the existing Celery task with ``SITE_URL`` links, never by
Django's PasswordResetView, which builds links from the request's Host header.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import FormView

from accounts.forms import EmailAuthenticationForm, NewPasswordForm, PasswordResetRequestForm
from accounts.models import EmailVerificationToken, LoginHistory
from accounts.services import queue_password_reset_email, reset_password, verify_email
from core import ratelimit
from core.audit import client_ip

User = get_user_model()

TOO_MANY_ATTEMPTS = "Too many attempts. Please wait a few minutes and try again."
RESET_SENT = (
    "If an account exists for that email, we've sent a link to reset the password. "
    "The link works once and expires in a few days."
)


def _limited(request, scope: str, rate: str, email: str) -> bool:
    """Count an attempt by client IP and by email; True when either is over ``rate``."""
    by_ip = ratelimit.hit(f"{scope}:ip", client_ip(request) or "unknown", rate)
    by_email = ratelimit.hit(f"{scope}:email", email, rate) if email else False
    return by_ip or by_email


class LoginView(auth_views.LoginView):
    form_class = EmailAuthenticationForm
    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        email = request.POST.get("username", "")
        if _limited(request, "login", settings.WEB_LOGIN_RATE, email):
            form = self.get_form()
            form.is_bound = False  # show the form again without validating the password
            context = self.get_context_data(form=form, rate_limited=TOO_MANY_ATTEMPTS)
            return self.render_to_response(context, status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        self._record(form.get_user(), successful=True)
        return response

    def form_invalid(self, form):
        user = User.objects.filter(email__iexact=form.data.get("username", "")).first()
        if user is not None:
            self._record(user, successful=False)
        return super().form_invalid(form)

    def _record(self, user, *, successful: bool) -> None:
        LoginHistory.objects.create(
            user=user,
            is_successful=successful,
            ip_address=client_ip(self.request),
            user_agent=self.request.META.get("HTTP_USER_AGENT", "")[:500],
        )


class LogoutView(auth_views.LogoutView):
    """POST only (Django's LogoutView refuses GET), so a link or image cannot sign you out."""

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        messages.success(request, "You've signed out.")
        return response


class PasswordResetRequestView(FormView):
    form_class = PasswordResetRequestForm
    template_name = "accounts/password_reset_request.html"
    success_url = reverse_lazy("login")

    def form_valid(self, form):
        email = form.cleaned_data["email"]
        if _limited(self.request, "password_reset", settings.WEB_PASSWORD_RESET_RATE, email):
            form.add_error(None, TOO_MANY_ATTEMPTS)
            return self.render_to_response(self.get_context_data(form=form), status=429)
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            queue_password_reset_email(user)
        # The same answer either way, so the page does not reveal which emails have accounts.
        messages.info(self.request, RESET_SENT)
        return super().form_valid(form)


class PasswordResetConfirmView(View):
    """``/reset-password/?uid=<id>&token=<token>``, the link from the reset email."""

    template_name = "accounts/password_reset_confirm.html"

    def _user(self, request):
        uid, token = request.GET.get("uid", ""), request.GET.get("token", "")
        user = User.objects.filter(pk=uid, is_active=True).first() if uid.isdigit() else None
        if user is None or not default_token_generator.check_token(user, token):
            return None
        return user

    def get(self, request):
        user = self._user(request)
        form = NewPasswordForm(user=user) if user else None
        return render(request, self.template_name, {"form": form}, status=200 if user else 400)

    def post(self, request):
        user = self._user(request)
        if user is None:
            return render(request, self.template_name, {"form": None}, status=400)
        form = NewPasswordForm(request.POST, user=user)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form}, status=400)
        reset_password(user, form.cleaned_data["new_password1"])
        messages.success(request, "Your password has been changed. Please sign in.")
        return redirect("login")


class VerifyEmailView(View):
    """``/verify-email/?token=<token>``. Verifying takes a click (POST): mail scanners that
    follow links with GET must not verify on the user's behalf."""

    template_name = "accounts/verify_email.html"

    def get(self, request):
        token = request.GET.get("token", "")
        valid = EmailVerificationToken.objects.filter(token=token).first()
        usable = valid is not None and valid.is_valid
        return render(
            request, self.template_name, {"usable": usable}, status=200 if usable else 400
        )

    def post(self, request):
        user = verify_email(request.GET.get("token", ""))
        if user is None:
            return render(request, self.template_name, {"usable": False}, status=400)
        messages.success(request, "Your email address is verified.")
        return redirect(reverse("home") if request.user.is_authenticated else reverse("login"))
