from __future__ import annotations

import logging
from functools import partial

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from core.audit import AuditAction, record_audit
from organizations.models import OrganizationInvitation, OrganizationMembership
from organizations.tasks import send_invitation_email

User = get_user_model()
logger = logging.getLogger(__name__)


@transaction.atomic
def accept_invitation(*, invitation: OrganizationInvitation, user: User) -> OrganizationMembership:
    """Join the organization with the invited role.

    An invitation never changes an existing active membership: accepting it as a current
    member (possibly the owner) would silently demote or promote them. Role changes are a
    separate, authorized operation. The invitation row is locked so it is used exactly once.
    """
    invitation = OrganizationInvitation.objects.select_for_update().get(pk=invitation.pk)
    if not invitation.is_usable:
        raise ValueError("Invitation is expired or already used")
    if not invitation.organization.accepts_members:
        raise ValueError("This organization isn't accepting new members right now")

    membership = (
        OrganizationMembership.objects.select_for_update()
        .filter(organization=invitation.organization, user=user)
        .first()
    )
    if membership is not None and membership.is_active:
        raise ValueError("You are already a member of this organization")
    if membership is None:
        membership = OrganizationMembership.objects.create(
            organization=invitation.organization, user=user, role=invitation.role
        )
    else:  # a former member, re-invited: reactivate with the invited role
        # Only the invited role: capability overrides from the old membership were granted
        # for a role and time that no longer apply, and the inviter couldn't have granted them.
        membership.role = invitation.role
        membership.is_active = True
        membership.granted_permissions = []
        membership.revoked_permissions = []
        membership.save(
            update_fields=[
                "role",
                "is_active",
                "granted_permissions",
                "revoked_permissions",
                "updated_at",
            ]
        )

    invitation.accepted_at = timezone.now()
    invitation.accepted_by = user
    invitation.save(update_fields=["accepted_at", "accepted_by", "updated_at"])

    record_audit(
        AuditAction.INVITATION_ACCEPTED,
        organization=invitation.organization,
        actor=user,
        target=invitation,
        metadata={"role": invitation.role},
    )
    return membership


def _enqueue_invitation_email(invitation_id: str) -> None:
    try:
        send_invitation_email.delay(invitation_id=invitation_id)
    except Exception:
        # Broker unavailable: the invitation exists and can be re-sent; never fail the request.
        logger.warning("Could not enqueue invitation email %s", invitation_id, exc_info=True)


@transaction.atomic
def create_invitation(
    *, organization, inviter, email: str, role: str, expires_at=None
) -> OrganizationInvitation:
    invitation = OrganizationInvitation.objects.create(
        organization=organization,
        inviter=inviter,
        email=email,
        role=role,
        token=OrganizationInvitation.generate_token(),
        expires_at=expires_at or OrganizationInvitation.default_expiry(),
    )
    record_audit(
        AuditAction.INVITATION_CREATED,
        organization=organization,
        actor=inviter,
        target=invitation,
        metadata={"role": role},
    )
    transaction.on_commit(partial(_enqueue_invitation_email, str(invitation.pk)))
    return invitation


@transaction.atomic
def register_and_accept_invitation(
    *, invitation: OrganizationInvitation, first_name: str, last_name: str, password: str
) -> OrganizationMembership:
    """Create the invited person's account and join the organization in one step.

    The account uses the invitation's email, and following the emailed link proves the
    mailbox, so the email is marked verified. If the invitation turns out to be used or
    expired, ``accept_invitation`` raises and the new account is rolled back with it.
    Raises ``IntegrityError`` when an account with that email already exists.
    """
    now = timezone.now()
    with transaction.atomic():
        user = User.objects.create_user(
            email=invitation.email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            email_verified=True,
            terms_accepted_at=now,
            privacy_accepted_at=now,
        )
    return accept_invitation(invitation=invitation, user=user)
