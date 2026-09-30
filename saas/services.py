"""Platform operations (M5.5): what ``/saas/`` does. Every write here is audited as a platform
admin action. None of it reads or changes an organization's customers or appointments.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from core.audit import AuditAction, record_audit
from core.models import AuditLog
from organizations.models import Organization, OrganizationRole
from organizations.services import create_invitation
from organizations.tenancy import reactivate_organization, suspend_organization
from subscriptions.models import Plan, Subscription

PLATFORM = AuditLog.ActorType.PLATFORM_ADMIN


def _audit(action, *, actor, organization=None, target=None, **metadata):
    record_audit(
        action,
        organization=organization,
        actor=actor,
        target=target,
        actor_type=PLATFORM,
        metadata=metadata,
    )


# -- Organizations ------------------------------------------------------------------------------


@transaction.atomic
def create_organization(
    *,
    name: str,
    owner_email: str,
    plan: Plan,
    slug: str = "",
    timezone_name: str = "UTC",
    currency: str = "USD",
    trial_days: int = 14,
    actor,
) -> Organization:
    """A new business: the organization (its default location follows), a subscription on
    ``plan`` (trialing for ``trial_days``, else active) and an owner invitation by email."""
    slug = slugify(slug or name)[:60]
    if not slug:
        raise ValidationError("Give the organization a name.")
    if Organization.objects.filter(slug=slug).exists():
        raise ValidationError(f"The address “{slug}” is taken. Choose another.")
    organization = Organization(
        name=name.strip(), slug=slug, timezone=timezone_name, currency=currency.upper()
    )
    organization.full_clean()
    organization.save()
    now = timezone.now()
    subscription = Subscription(organization=organization, plan=plan)
    if trial_days:
        subscription.status = Subscription.Status.TRIALING
        subscription.trial_start = now
        subscription.trial_end = now + timedelta(days=trial_days)
    else:
        subscription.status = Subscription.Status.ACTIVE
        subscription.current_period_start = now
        subscription.current_period_end = now + timedelta(days=30)
    subscription.save()
    _audit(
        AuditAction.ORGANIZATION_CREATED,
        actor=actor,
        organization=organization,
        target=organization,
        plan=plan.slug,
        trial_days=trial_days,
    )
    create_invitation(
        organization=organization,
        inviter=actor,
        email=owner_email.strip().lower(),
        role=OrganizationRole.OWNER,
    )
    return organization


def suspend(*, organization: Organization, reason: str, actor) -> None:
    reason = reason.strip()
    if not reason:
        raise ValidationError("Give a reason: the owner sees it.")
    suspend_organization(
        organization=organization, reason=reason[:255], actor=actor, actor_type=PLATFORM
    )


def reactivate(*, organization: Organization, reason: str, actor) -> None:
    reason = reason.strip()
    if not reason:
        raise ValidationError("Give a reason for the record.")
    reactivate_organization(
        organization=organization, actor=actor, reason=reason[:255], actor_type=PLATFORM
    )


# -- Subscriptions and plans --------------------------------------------------------------------


@transaction.atomic
def change_subscription(*, subscription: Subscription, actor, **changes) -> Subscription:
    """Change the plan, status, billing cycle or trial end (no payment provider yet)."""
    allowed = {"plan", "status", "billing_cycle", "trial_end", "current_period_end"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValidationError(f"Unknown fields: {', '.join(sorted(unknown))}")
    subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
    before = {field: getattr(subscription, field) for field in changes}
    for field, value in changes.items():
        setattr(subscription, field, value)
    subscription.full_clean()
    subscription.save()
    diff = {
        field: {"from": _plain(before[field]), "to": _plain(value)}
        for field, value in changes.items()
        if before[field] != value
    }
    if diff:
        _audit(
            AuditAction.SUBSCRIPTION_CHANGED,
            actor=actor,
            organization=subscription.organization,
            target=subscription,
            changes=diff,
        )
    return subscription


def _plain(value):
    if isinstance(value, Plan):
        return value.slug
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


PLAN_FIELDS = (
    "name",
    "slug",
    "monthly_price",
    "yearly_price",
    "maximum_staff",
    "maximum_services",
    "maximum_monthly_bookings",
    "maximum_locations",
    "analytics_enabled",
    "api_access_enabled",
    "is_active",
)


@transaction.atomic
def save_plan(*, plan: Plan | None = None, actor, **fields) -> Plan:
    created = plan is None
    plan = plan or Plan()
    before = {field: getattr(plan, field) for field in PLAN_FIELDS} if not created else {}
    for field, value in fields.items():
        if field not in PLAN_FIELDS:
            raise ValidationError(f"Unknown field: {field}")
        setattr(plan, field, value)
    plan.full_clean()
    plan.save()
    changes = {
        field: str(getattr(plan, field))
        for field in PLAN_FIELDS
        if created or before[field] != getattr(plan, field)
    }
    _audit(
        AuditAction.PLAN_CREATED if created else AuditAction.PLAN_UPDATED,
        actor=actor,
        target=plan,
        changes=changes,
    )
    return plan


# -- Users --------------------------------------------------------------------------------------


def set_user_active(*, user, active: bool, reason: str, actor) -> None:
    """Deactivate (can't sign in; their data stays) or reactivate an account."""
    if user.pk == actor.pk:
        raise PermissionDenied("You can't change your own account here.")
    if user.is_platform_user and not actor.is_superuser:
        raise PermissionDenied("Only a superuser can change another platform account.")
    reason = reason.strip()
    if not reason:
        raise ValidationError("Give a reason for the record.")
    if user.is_active == active:
        return
    user.is_active = active
    user.save(update_fields=["is_active"])
    _audit(
        AuditAction.USER_REACTIVATED if active else AuditAction.USER_DEACTIVATED,
        actor=actor,
        target=user,
        reason=reason[:255],
    )


def set_platform_staff(*, user, value: bool, actor) -> None:
    if not actor.is_superuser:
        raise PermissionDenied("Only a superuser can grant or remove platform access.")
    if user.pk == actor.pk:
        raise PermissionDenied("You can't change your own platform access.")
    if user.is_platform_staff == value:
        return
    user.is_platform_staff = value
    user.save(update_fields=["is_platform_staff"])
    _audit(
        AuditAction.PLATFORM_STAFF_GRANTED if value else AuditAction.PLATFORM_STAFF_REVOKED,
        actor=actor,
        target=user,
    )
