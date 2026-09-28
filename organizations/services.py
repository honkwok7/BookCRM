from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from core.services import write_audit_log
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

    write_audit_log(
        action="invitation.accepted",
        organization=invitation.organization,
        user=user,
        object_type="OrganizationInvitation",
        object_identifier=str(invitation.id),
        metadata={"role": invitation.role, "email": invitation.email},
    )
    return membership
