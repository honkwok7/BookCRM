"""The platform admin pages (M5.5): ``/saas/``. Platform staff and superusers only
(``PlatformAdminMixin``); everyone else gets 403.

These pages show platform facts: organizations, subscriptions, plans, accounts and the audit
trail, with counts of what each organization holds. They never list an organization's
customers or appointments; that data is reachable only through impersonation.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from bookings.models import Booking, Customer
from core.audit import AuditAction
from core.models import AuditLog
from core.web import PlatformAdminMixin
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from saas import impersonation, services
from saas.forms import (
    AnnouncementForm,
    FlagForm,
    ImpersonateForm,
    OrganizationCreateForm,
    OverrideForm,
    PlanForm,
    ReasonForm,
    SubscriptionForm,
)
from saas.metrics import monthly_value, platform_overview
from saas.models import Announcement, FeatureFlag, ImpersonationSession
from staff.models import StaffProfile
from subscriptions.models import Plan, Subscription

User = get_user_model()
PAGE = 25
SECTIONS = (
    ("overview", "Overview", "saas-dashboard"),
    ("organizations", "Organizations", "saas-organizations"),
    ("subscriptions", "Subscriptions", "saas-subscriptions"),
    ("plans", "Plans", "saas-plans"),
    ("users", "Users", "saas-users"),
    ("announcements", "Announcements", "saas-announcements"),
    ("flags", "Feature flags", "saas-flags"),
    ("audit", "Audit log", "saas-audit"),
)


def error_text(error) -> str:
    return " ".join(getattr(error, "messages", None) or [str(error)])


class PlatformPage(PlatformAdminMixin):
    section = ""

    def page(self, template, context=None, *, status=200):
        return render(
            self.request,
            template,
            {"section": self.section, "saas_sections": SECTIONS, **(context or {})},
            status=status,
        )


class DashboardView(PlatformPage, View):
    section = "overview"

    def get(self, request):
        return self.page("saas/dashboard.html", {"overview": platform_overview()})


# -- Organizations ------------------------------------------------------------------------------


class OrganizationListView(PlatformPage, View):
    section = "organizations"
    STATES = {
        "active": Q(is_active=True, is_suspended=False),
        "suspended": Q(is_suspended=True),
        "trialing": Q(subscription__status=Subscription.Status.TRIALING),
        "past_due": Q(subscription__status=Subscription.Status.PAST_DUE),
    }

    def get(self, request):
        query = request.GET.get("q", "").strip()
        state = request.GET.get("state", "")
        organizations = (
            Organization.objects.select_related("subscription__plan")
            .annotate(members=Count("memberships", filter=Q(memberships__is_active=True)))
            .order_by("name")
        )
        if query:
            organizations = organizations.filter(
                Q(name__icontains=query) | Q(slug__icontains=query)
            )
        if state in self.STATES:
            organizations = organizations.filter(self.STATES[state])
        page = Paginator(organizations, PAGE).get_page(request.GET.get("page"))
        return self.page(
            "saas/organizations.html",
            {"page_obj": page, "query": query, "state": state, "states": list(self.STATES)},
        )


class OrganizationCreateView(PlatformPage, View):
    section = "organizations"

    def get(self, request):
        return self.page("saas/organization_form.html", {"form": OrganizationCreateForm()})

    def post(self, request):
        form = OrganizationCreateForm(request.POST)
        if form.is_valid():
            try:
                organization = services.create_organization(actor=request.user, **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error_text(error))
            else:
                messages.success(request, f"{organization.name} created; the owner is invited.")
                return redirect("saas-organization", pk=organization.pk)
        return self.page("saas/organization_form.html", {"form": form}, status=422)


class OrganizationDetailView(PlatformPage, View):
    section = "organizations"

    def get_organization(self, pk) -> Organization:
        organization = (
            Organization.objects.select_related("subscription__plan").filter(pk=pk).first()
        )
        if organization is None:
            raise Http404("Organization not found")
        return organization

    def context(self, organization, **extra):
        since = timezone.now() - timedelta(days=30)
        members = OrganizationMembership.objects.filter(organization=organization, is_active=True)
        subscription = getattr(organization, "subscription", None)
        return {
            "organization": organization,
            "subscription": subscription,
            "roles": list(members.values("role").annotate(count=Count("pk")).order_by("role")),
            "owners": list(members.filter(role=OrganizationRole.OWNER).select_related("user")),
            "invitations": list(
                OrganizationInvitation.objects.filter(
                    organization=organization, accepted_at__isnull=True
                ).order_by("-created_at")[:5]
            ),
            # Counts only: the platform never lists an organization's records.
            "counts": {
                "staff": StaffProfile.objects.filter(organization=organization).count(),
                "customers": Customer.objects.filter(organization=organization).count(),
                "appointments": Booking.objects.filter(organization=organization).count(),
                "appointments_recent": Booking.objects.filter(
                    organization=organization, created_at__gte=since
                ).count(),
            },
            "history": list(
                AuditLog.objects.filter(
                    organization=organization, actor_type=AuditLog.ActorType.PLATFORM_ADMIN
                )
                .select_related("user")
                .order_by("-created_at")[:10]
            ),
            "reason_form": extra.pop("reason_form", ReasonForm()),
            "subscription_form": extra.pop(
                "subscription_form",
                SubscriptionForm(instance=subscription) if subscription else None,
            ),
            **extra,
        }

    def get(self, request, pk):
        organization = self.get_organization(pk)
        return self.page("saas/organization.html", self.context(organization))


class OrganizationStatusView(OrganizationDetailView):
    """Suspend or reactivate, with a reason (audited)."""

    def post(self, request, pk, action):
        if action not in ("suspend", "reactivate"):
            raise Http404("Unknown action")
        organization = self.get_organization(pk)
        form = ReasonForm(request.POST)
        if form.is_valid():
            change = services.suspend if action == "suspend" else services.reactivate
            try:
                change(
                    organization=organization,
                    reason=form.cleaned_data["reason"],
                    actor=request.user,
                )
            except ValidationError as error:
                form.add_error(None, error_text(error))
            else:
                messages.success(
                    request,
                    f"{organization.name} {'suspended' if action == 'suspend' else 'reactivated'}.",
                )
                return redirect("saas-organization", pk=pk)
        return self.page(
            "saas/organization.html", self.context(organization, reason_form=form), status=422
        )


class SubscriptionChangeView(OrganizationDetailView):
    def post(self, request, pk):
        organization = self.get_organization(pk)
        subscription = getattr(organization, "subscription", None)
        if subscription is None:
            raise Http404("No subscription")
        form = SubscriptionForm(request.POST, instance=Subscription.objects.get(pk=subscription.pk))
        if form.is_valid():
            changes = {field: form.cleaned_data[field] for field in form.changed_data}
            try:
                services.change_subscription(
                    subscription=subscription, actor=request.user, **changes
                )
            except ValidationError as error:
                form.add_error(None, error_text(error))
            else:
                messages.success(request, "Subscription saved.")
                return redirect("saas-organization", pk=pk)
        return self.page(
            "saas/organization.html",
            self.context(organization, subscription_form=form),
            status=422,
        )


# -- Subscriptions and plans --------------------------------------------------------------------


class SubscriptionListView(PlatformPage, View):
    section = "subscriptions"

    def get(self, request):
        status = request.GET.get("status", "")
        subscriptions = (
            Subscription.objects.select_related("organization", "plan")
            .annotate(monthly=monthly_value())
            .order_by("organization__name")
        )
        if status in Subscription.Status.values:
            subscriptions = subscriptions.filter(status=status)
        page = Paginator(subscriptions, PAGE).get_page(request.GET.get("page"))
        return self.page(
            "saas/subscriptions.html",
            {"page_obj": page, "status": status, "statuses": Subscription.Status.choices},
        )


class PlanListView(PlatformPage, View):
    section = "plans"

    def get(self, request):
        plans = Plan.objects.annotate(
            organizations=Count(
                "subscriptions",
                filter=~Q(
                    subscriptions__status__in=(
                        Subscription.Status.CANCELLED,
                        Subscription.Status.EXPIRED,
                    )
                ),
            )
        ).order_by("monthly_price", "name")
        return self.page("saas/plans.html", {"plans": plans})


class PlanFormView(PlatformPage, View):
    section = "plans"

    def get_plan(self, pk):
        if pk is None:
            return None
        plan = Plan.objects.filter(pk=pk).first()
        if plan is None:
            raise Http404("Plan not found")
        return plan

    def get(self, request, pk=None):
        plan = self.get_plan(pk)
        return self.page("saas/plan_form.html", {"plan": plan, "form": PlanForm(instance=plan)})

    def post(self, request, pk=None):
        plan = self.get_plan(pk)
        form = PlanForm(request.POST, instance=Plan.objects.get(pk=plan.pk) if plan else None)
        if form.is_valid():
            try:
                services.save_plan(plan=plan, actor=request.user, **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error_text(error))
            else:
                messages.success(request, "Plan saved.")
                return redirect("saas-plans")
        return self.page("saas/plan_form.html", {"plan": plan, "form": form}, status=422)


# -- Users --------------------------------------------------------------------------------------


class UserListView(PlatformPage, View):
    section = "users"

    def get(self, request):
        query = request.GET.get("q", "").strip()
        users = User.objects.annotate(
            organizations=Count(
                "organization_memberships",
                filter=Q(organization_memberships__is_active=True),
            )
        ).order_by("-date_joined")
        if query:
            users = users.filter(
                Q(email__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
            )
        page = Paginator(users, PAGE).get_page(request.GET.get("page"))
        return self.page("saas/users.html", {"page_obj": page, "query": query})


class UserDetailView(PlatformPage, View):
    section = "users"

    def get_user(self, pk):
        user = User.objects.filter(pk=pk).first()
        if user is None:
            raise Http404("User not found")
        return user

    def context(self, account, **extra):
        return {
            "account": account,
            "memberships": list(
                OrganizationMembership.objects.filter(user=account)
                .select_related("organization")
                .order_by("organization__name")
            ),
            "reason_form": extra.pop("reason_form", ReasonForm()),
            "sessions": list(
                ImpersonationSession.objects.filter(target_user=account)
                .select_related("admin", "organization")
                .order_by("-started_at")[:10]
            ),
            "can_impersonate": account.is_active
            and not account.is_platform_user
            and account.pk != self.request.user.pk,
            **extra,
        }

    def get(self, request, pk):
        return self.page("saas/user.html", self.context(self.get_user(pk)))

    def post(self, request, pk):
        account = self.get_user(pk)
        action = request.POST.get("action")
        form = ReasonForm(request.POST)
        try:
            if action in ("grant_platform", "revoke_platform"):
                services.set_platform_staff(
                    user=account, value=action == "grant_platform", actor=request.user
                )
                messages.success(request, "Platform access changed.")
                return redirect("saas-user", pk=pk)
            if action in ("deactivate", "reactivate") and form.is_valid():
                services.set_user_active(
                    user=account,
                    active=action == "reactivate",
                    reason=form.cleaned_data["reason"],
                    actor=request.user,
                )
                messages.success(request, "Account updated.")
                return redirect("saas-user", pk=pk)
        except PermissionDenied as error:
            form.add_error(None, str(error))
            return self.page("saas/user.html", self.context(account, reason_form=form), status=403)
        except ValidationError as error:
            form.add_error(None, error_text(error))
        return self.page("saas/user.html", self.context(account, reason_form=form), status=422)


# -- Audit log ----------------------------------------------------------------------------------


class AuditLogView(PlatformPage, View):
    """Every audited action, platform-wide. Details (metadata) are shown for platform actions
    only: an organization's own entries can describe its customers."""

    section = "audit"

    def get(self, request):
        action = request.GET.get("action", "")
        organization = request.GET.get("organization", "").strip()
        platform_only = request.GET.get("platform") == "1"
        entries = AuditLog.objects.select_related("organization", "user").order_by("-created_at")
        if action in {a.value for a in AuditAction}:
            entries = entries.filter(action=action)
        if organization:
            entries = entries.filter(organization__slug=organization)
        if platform_only:
            entries = entries.filter(actor_type=AuditLog.ActorType.PLATFORM_ADMIN)
        page = Paginator(entries, 50).get_page(request.GET.get("page"))
        return self.page(
            "saas/audit_log.html",
            {
                "page_obj": page,
                "action": action,
                "organization_slug": organization,
                "platform_only": platform_only,
                "actions": sorted(a.value for a in AuditAction),
                "platform_type": AuditLog.ActorType.PLATFORM_ADMIN,
            },
        )


# -- Announcements, feature flags, impersonation (M5.5b) --------------------------------------


class AnnouncementListView(PlatformPage, View):
    section = "announcements"

    def get(self, request):
        announcements = Announcement.objects.select_related("created_by")
        return self.page("saas/announcements.html", {"announcements": announcements})


class AnnouncementFormView(PlatformPage, View):
    section = "announcements"

    def get_announcement(self, pk):
        if pk is None:
            return None
        announcement = Announcement.objects.filter(pk=pk).first()
        if announcement is None:
            raise Http404("Announcement not found")
        return announcement

    def get(self, request, pk=None):
        announcement = self.get_announcement(pk)
        return self.page(
            "saas/announcement_form.html",
            {"announcement": announcement, "form": AnnouncementForm(instance=announcement)},
        )

    def post(self, request, pk=None):
        announcement = self.get_announcement(pk)
        form = AnnouncementForm(
            request.POST,
            instance=Announcement.objects.get(pk=announcement.pk) if announcement else None,
        )
        if form.is_valid():
            try:
                services.save_announcement(
                    announcement=announcement, actor=request.user, **form.cleaned_data
                )
            except ValidationError as error:
                form.add_error(None, error_text(error))
            else:
                messages.success(request, "Announcement saved.")
                return redirect("saas-announcements")
        return self.page(
            "saas/announcement_form.html",
            {"announcement": announcement, "form": form},
            status=422,
        )


class FlagListView(PlatformPage, View):
    section = "flags"

    def render_list(self, form, *, status=200):
        flags = FeatureFlag.objects.annotate(override_count=Count("overrides"))
        return self.page("saas/flags.html", {"flags": flags, "form": form}, status=status)

    def get(self, request):
        return self.render_list(FlagForm())

    def post(self, request):
        form = FlagForm(request.POST)
        if form.is_valid():
            try:
                flag = services.save_flag(actor=request.user, **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error_text(error))
            else:
                messages.success(request, f"Flag {flag.key} created.")
                return redirect("saas-flag", pk=flag.pk)
        return self.render_list(form, status=422)


class FlagDetailView(PlatformPage, View):
    section = "flags"

    def get_flag(self, pk):
        flag = FeatureFlag.objects.filter(pk=pk).first()
        if flag is None:
            raise Http404("Flag not found")
        return flag

    def render_flag(self, flag, *, flag_form=None, override_form=None, status=200):
        initial = {"key": flag.key, "description": flag.description, "enabled": flag.enabled}
        return self.page(
            "saas/flag.html",
            {
                "flag": flag,
                "flag_form": flag_form or FlagForm(initial=initial),
                "override_form": override_form or OverrideForm(),
                "overrides": flag.overrides.select_related("organization").order_by(
                    "organization__name"
                ),
            },
            status=status,
        )

    def get(self, request, pk):
        return self.render_flag(self.get_flag(pk))

    def post(self, request, pk):
        flag = self.get_flag(pk)
        if "organization" in request.POST:
            form = OverrideForm(request.POST)
            if form.is_valid():
                organization = Organization.objects.filter(
                    slug=form.cleaned_data["organization"]
                ).first()
                if organization is None:
                    form.add_error("organization", "No organization has that address.")
                else:
                    state = form.cleaned_data["state"]
                    services.set_flag_override(
                        flag=flag,
                        organization=organization,
                        enabled=None if state == "default" else state == "on",
                        actor=request.user,
                    )
                    messages.success(request, "Override saved.")
                    return redirect("saas-flag", pk=pk)
            return self.render_flag(flag, override_form=form, status=422)
        form = FlagForm(request.POST)
        if form.is_valid():
            try:
                services.save_flag(flag=flag, actor=request.user, **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error_text(error))
            else:
                messages.success(request, "Flag saved.")
                return redirect("saas-flag", pk=pk)
        return self.render_flag(flag, flag_form=form, status=422)


class ImpersonateView(UserDetailView):
    """Start impersonating ``pk`` in one of their organizations. Step-up: the admin types their
    password again. The form has its own page, away from the account page."""

    def organizations(self, account):
        return Organization.objects.filter(
            memberships__user=account,
            memberships__is_active=True,
            is_active=True,
            is_suspended=False,
        ).order_by("name")

    def render_form(self, account, form, *, status=200):
        return self.page("saas/impersonate.html", {"account": account, "form": form}, status=status)

    def get(self, request, pk):
        account = self.get_user(pk)
        return self.render_form(account, ImpersonateForm(organizations=self.organizations(account)))

    def post(self, request, pk):
        account = self.get_user(pk)
        form = ImpersonateForm(request.POST, organizations=self.organizations(account))
        if form.is_valid():
            data = form.cleaned_data
            try:
                impersonation.start(
                    request=request,
                    admin=request.user,
                    target=account,
                    organization=data["organization"],
                    reason=data["reason"],
                    password=data["password"],
                    minutes=data["minutes"],
                )
            except PermissionDenied as error:
                form.add_error(None, str(error))
                return self.render_form(account, form, status=403)
            except ValidationError as error:
                form.add_error(None, error_text(error))
            else:
                return redirect("home")
        return self.render_form(account, form, status=422)
