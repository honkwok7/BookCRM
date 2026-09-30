"""Account emails, sent by Celery after the request's transaction commits.

No secret crosses the broker: the verification token is read from (or created in) the
database and the password reset token is generated in the worker.

"Forgot password" and "resend verification" queue the task with the email address the visitor
typed, whether or not an account has it. The worker looks the account up and does nothing when
there's no email to send, so the request does the same work, and takes the same time, either
way: its timing doesn't reveal which addresses have accounts.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail

from accounts.models import EmailVerificationToken
from config.celery import app


@app.task(bind=True, max_retries=3)
def send_verification_email(self, *, token_id=None, email=""):
    if not token_id:
        # Resend: only for an active account whose email isn't verified yet.
        user = (
            get_user_model()
            .objects.filter(email__iexact=email, is_active=True, email_verified=False)
            .first()
        )
        if user is None:
            return
        token_id = EmailVerificationToken.objects.create(
            user=user,
            token=EmailVerificationToken.generate_token(),
            expires_at=EmailVerificationToken.default_expires_at(),
        ).pk
    token = EmailVerificationToken.objects.select_related("user").get(pk=token_id)
    url = f"{settings.SITE_URL}/verify-email/?token={token.token}"
    try:
        send_mail(
            subject=f"Verify your {settings.SITE_NAME} account",
            message=f"Use this link to verify your email: {url}",
            from_email=None,
            recipient_list=[token.user.email],
        )
    except Exception as exc:  # pragma: no cover
        raise self.retry(exc=exc, countdown=30) from exc


@app.task(bind=True, max_retries=3)
def send_password_reset_email(self, *, email="", user_id=None):
    users = get_user_model().objects.filter(is_active=True)
    user = users.filter(pk=user_id).first() if user_id else None
    if user is None and email:
        user = users.filter(email__iexact=email).first()
    if user is None:
        return  # no such account: nothing to send
    token = default_token_generator.make_token(user)
    url = f"{settings.SITE_URL}/reset-password/?uid={user.pk}&token={token}"
    try:
        send_mail(
            subject=f"Reset your {settings.SITE_NAME} password",
            message=f"Use this link to reset your password: {url}",
            from_email=None,
            recipient_list=[user.email],
        )
    except Exception as exc:  # pragma: no cover
        raise self.retry(exc=exc, countdown=30) from exc
