import logging
from functools import partial

from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from accounts.models import EmailVerificationToken
from accounts.tasks import send_password_reset_email, send_verification_email
from core.audit import AuditAction, record_audit

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


def verify_email(token_value: str):
    """Mark the token's account verified; return the user, or None for a bad/expired token."""
    with transaction.atomic():
        token = (
            EmailVerificationToken.objects.select_for_update()
            .select_related("user")
            .filter(token=token_value)
            .first()
        )
        if token is None or not token.is_valid:
            return None
        token.used_at = timezone.now()
        token.save(update_fields=["used_at", "updated_at"])
        user = token.user
        user.email_verified = True
        user.save(update_fields=["email_verified"])
        record_audit(AuditAction.ACCOUNT_EMAIL_VERIFIED, actor=user, target=user)
    return user


@transaction.atomic
def reset_password(user, new_password: str) -> None:
    """Set a new password and sign the account out everywhere.

    Web sessions end because Django ties them to the password hash; access tokens end through
    SIMPLE_JWT ``CHECK_REVOKE_TOKEN``; refresh tokens are blacklisted here.
    """
    user.set_password(new_password)
    user.save(update_fields=["password"])
    revoked = revoke_refresh_tokens(user)
    record_audit(
        AuditAction.ACCOUNT_PASSWORD_RESET,
        actor=user,
        target=user,
        metadata={"refresh_tokens_revoked": revoked},
    )


@transaction.atomic
def reset_password_with_token(*, user_id, token: str, new_password: str) -> bool:
    """Use a password-reset link: returns False if the link is invalid, expired or used.

    The account row is locked and the token checked again under the lock. The token is tied
    to the password hash, so once one request has changed the password, a concurrent request
    with the same link fails here instead of overwriting it.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.tokens import default_token_generator

    user = get_user_model().objects.select_for_update().filter(pk=user_id, is_active=True).first()
    if user is None or not default_token_generator.check_token(user, token):
        return False
    reset_password(user, new_password)
    return True
