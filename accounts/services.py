import logging
from functools import partial

from django.db import transaction
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from accounts.models import EmailVerificationToken
from accounts.tasks import send_password_reset_email, send_verification_email

logger = logging.getLogger(__name__)


def _enqueue(task, **kwargs) -> None:
    try:
        task.delay(**kwargs)
    except Exception:
        # Broker unavailable: the user can ask for the email again; never fail the request.
        logger.warning("Could not enqueue %s", task.name, exc_info=True)


def queue_verification_email(user) -> EmailVerificationToken:
    token = EmailVerificationToken.objects.create(
        user=user,
        token=EmailVerificationToken.generate_token(),
        expires_at=EmailVerificationToken.default_expires_at(),
    )
    transaction.on_commit(partial(_enqueue, send_verification_email, token_id=str(token.pk)))
    return token


def queue_password_reset_email(user) -> None:
    transaction.on_commit(partial(_enqueue, send_password_reset_email, user_id=user.pk))


def revoke_refresh_tokens(user) -> int:
    """Blacklist every outstanding refresh token, e.g. after a password reset.

    Access tokens are invalidated by the password change itself (SIMPLE_JWT
    ``CHECK_REVOKE_TOKEN``): they carry a fingerprint of the old password hash.
    """
    revoked = 0
    for token in OutstandingToken.objects.filter(user=user):
        _, created = BlacklistedToken.objects.get_or_create(token=token)
        revoked += created
    return revoked
