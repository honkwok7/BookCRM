"""Impersonation (M5.5b): a platform admin sees the app as one user, in one organization, for a
limited time.

- Starting it needs the admin's password again (step-up), a reason, and a target who is an
  active member of that organization and not a platform account.
- ``ImpersonationMiddleware`` swaps ``request.user`` for web pages (session sign-in only) and
  keeps the admin on ``request.impersonator``: ``core.audit.record_audit`` stores them as the
  impersonator of every audit row, and each change request (POST and friends) is audited too.
- The API, the Django admin, the platform pages and account pages (password reset and so on)
  are refused while it runs; signing out ends it instead.
- It ends when the admin ends it or the time is up (checked on every request).
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import redirect
from django.utils import timezone

from core.audit import AuditAction, client_ip, record_audit
from core.models import AuditLog
from organizations.models import OrganizationMembership
from organizations.tenancy import SESSION_KEY, requested_organization_slug
from saas.models import ImpersonationSession

SESSION_ID = "impersonation_session"
DURATIONS = (15, 30, 60)  # minutes
BLOCKED_PREFIXES = (
    "/api/",
    "/admin/",
    "/saas/",
    "/password-reset/",
    "/reset-password/",
    "/app/switch/",  # one organization only
)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")


def start(*, request, admin, target, organization, reason: str, password: str, minutes: int):
    if not admin.check_password(password or ""):
        raise PermissionDenied("That password is not right.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give a reason: it is kept with the session.")
    if minutes not in DURATIONS:
        raise ValidationError("Choose how long.")
    if target.pk == admin.pk or target.is_superuser or target.is_platform_staff:
        raise PermissionDenied("Platform accounts can't be impersonated.")
    if not target.is_active:
        raise PermissionDenied("This account is deactivated.")
    if not OrganizationMembership.objects.filter(
        user=target, organization=organization, is_active=True
    ).exists():
        raise ValidationError("They aren't an active member of that organization.")
    if organization.is_suspended or not organization.is_active:
        raise ValidationError("That organization is suspended or inactive.")
    now = timezone.now()
    with transaction.atomic():
        session = ImpersonationSession.objects.create(
            admin=admin,
            target_user=target,
            organization=organization,
            reason=reason[:255],
            started_at=now,
            expires_at=now + timedelta(minutes=minutes),
            ip_address=client_ip(request),
        )
        record_audit(
            AuditAction.IMPERSONATION_STARTED,
            organization=organization,
            actor=admin,
            actor_type=AuditLog.ActorType.PLATFORM_ADMIN,
            target=target,
            metadata={"session": str(session.pk), "reason": session.reason, "minutes": minutes},
            request=request,
        )
    request.session[SESSION_ID] = str(session.pk)
    request.session[SESSION_KEY] = str(organization.pk)
    return session


def end(request, session: ImpersonationSession, *, why=ImpersonationSession.EndReason.ENDED):
    with transaction.atomic():
        locked = ImpersonationSession.objects.select_for_update().get(pk=session.pk)
        if locked.ended_at is None:
            locked.ended_at = timezone.now()
            locked.end_reason = why
            locked.save(update_fields=["ended_at", "end_reason", "updated_at"])
            record_audit(
                AuditAction.IMPERSONATION_ENDED,
                organization=locked.organization,
                actor=locked.admin,
                actor_type=AuditLog.ActorType.PLATFORM_ADMIN,
                target=locked.target_user,
                metadata={"session": str(locked.pk), "why": why},
                request=request,
            )
    request.session.pop(SESSION_ID, None)
    request.session.pop(SESSION_KEY, None)


class ImpersonationMiddleware:
    """After AuthenticationMiddleware. Does nothing (no query) unless the session holds an
    impersonation."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.impersonator = None
        request.impersonation = None
        session_id = request.session.get(SESSION_ID) if hasattr(request, "session") else None
        if session_id and request.user.is_authenticated:
            response = self.impersonate(request, session_id)
            if response is not None:
                return response
        return self.get_response(request)

    def impersonate(self, request, session_id):
        session = (
            ImpersonationSession.objects.select_related("target_user", "organization", "admin")
            .filter(pk=session_id, admin=request.user)
            .first()
        )
        if session is None:
            request.session.pop(SESSION_ID, None)
            return None
        if (
            not session.is_live()
            or not request.user.is_platform_user
            or not session.target_user.is_active
        ):
            why = (
                ImpersonationSession.EndReason.EXPIRED
                if session.ended_at is None and timezone.now() >= session.expires_at
                else ImpersonationSession.EndReason.REVOKED
            )
            end(request, session, why=why)
            messages.info(request, "The impersonation session has ended.")
            return redirect("saas-dashboard") if request.user.is_platform_user else None
        requested = requested_organization_slug(request)
        if request.path.startswith(BLOCKED_PREFIXES) or (
            requested and requested != session.organization.slug
        ):
            # The usual 403 page (middleware exceptions become responses too).
            raise PermissionDenied("Not available while impersonating.")
        request.session[SESSION_KEY] = str(session.organization_id)
        if request.path == "/logout/" and request.method == "POST":
            end(request, session)
            return redirect("saas-user", pk=session.target_user_id)
        admin = request.user
        request.user = session.target_user
        request.impersonator = admin
        request.impersonation = session
        if request.method not in SAFE_METHODS and request.path != "/impersonation/end/":
            record_audit(
                AuditAction.IMPERSONATION_REQUEST,
                organization=session.organization,
                actor=session.target_user,
                target=session,
                metadata={"method": request.method, "path": request.path[:255]},
                request=request,
            )
        return None


def end_view(request):
    """POST /impersonation/end/: the banner's End button."""
    session = getattr(request, "impersonation", None)
    if request.method != "POST" or session is None:
        return redirect("home")
    request.user, request.impersonator = request.impersonator, None
    end(request, session)
    messages.success(request, "Impersonation ended.")
    return redirect("saas-user", pk=session.target_user_id)
