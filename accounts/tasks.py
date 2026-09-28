"""Account emails, sent by Celery after the request's transaction commits.

Only ids cross the broker: the verification token is read from the database and the password
reset token is generated in the worker, so no secret sits in a queue.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail

from accounts.models import EmailVerificationToken
from config.celery import app


@app.task(bind=True, max_retries=3)
def send_verification_email(self, *, token_id):
    token = EmailVerificationToken.objects.select_related("user").get(pk=token_id)
    url = f"{settings.SITE_URL}/verify-email/?token={token.token}"
    try:
        send_mail(
            subject="Verify your Schedula account",
            message=f"Use this link to verify your email: {url}",
            from_email=None,
            recipient_list=[token.user.email],
        )
    except Exception as exc:  # pragma: no cover
        raise self.retry(exc=exc, countdown=30) from exc


@app.task(bind=True, max_retries=3)
def send_password_reset_email(self, *, user_id):
    user = get_user_model().objects.get(pk=user_id)
    token = default_token_generator.make_token(user)
    url = f"{settings.SITE_URL}/reset-password/?uid={user.pk}&token={token}"
    try:
        send_mail(
            subject="Reset your Schedula password",
            message=f"Use this link to reset your password: {url}",
            from_email=None,
            recipient_list=[user.email],
        )
    except Exception as exc:  # pragma: no cover
        raise self.retry(exc=exc, countdown=30) from exc
