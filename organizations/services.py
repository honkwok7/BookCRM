from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from core.audit import AuditAction, record_audit
from organizations.models import OrganizationInvitation, OrganizationMembership

User = get_user_model()


@transaction.atomic
def accept_invitation(*, invitation: OrganizationInvitation, user: User) -> OrganizationMembership:
    if not invitation.is_usable:
        raise ValueError("Invitation is expired or already used")

    membership, _ = OrganizationMembership.objects.get_or_create(
        organization=invitation.organization,
        user=user,
        defaults={"role": invitation.role, "is_active": True},
    )
    if membership.role != invitation.role:
        membership.role = invitation.role
        membership.is_active = True
        membership.save(update_fields=["role", "is_active", "updated_at"])

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


def create_invitation(
    *, organization, inviter, email: str, role: str, expires_at=None, accept_base_url: str
) -> OrganizationInvitation:
    with transaction.atomic():
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
    # Synchronous for now; moves onto the notification pipeline in M7.1.
    send_mail(
        subject=f"Invitation to join {organization.name}",
        message=(
            f"You were invited to join {organization.name}. "
            f"Accept invitation: {accept_base_url}accept-invitation/?token={invitation.token}"
        ),
        from_email=None,
        recipient_list=[invitation.email],
    )
    return invitation
