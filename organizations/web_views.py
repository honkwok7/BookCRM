from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views import View
from django.views.generic import TemplateView

from core.web import HtmxPartialMixin, TenantPageMixin
from organizations.forms import InvitedSignupForm
from organizations.models import OrganizationInvitation, OrganizationMembership, OrganizationRole
from organizations.permissions import Capability
from organizations.services import accept_invitation, register_and_accept_invitation
from organizations.tenancy import set_active_organization

User = get_user_model()

ACCOUNT_EXISTS = "An account with this email already exists. Sign in to accept the invitation."


class AcceptInvitationView(View):
    """``/accept-invitation/?token=<token>``, the link from the invitation email.

    - Signed in with the invited email: confirm and join.
    - Signed in as someone else: explain, offer to sign out (never join the wrong account).
    - Signed out, account exists: sign in first, then come back here.
    - Signed out, no account: create one (email fixed to the invitation) and join.
    """

    template_name = "organizations/accept_invitation.html"

    def dispatch(self, request, *args, **kwargs):
        self.invitation = (
            OrganizationInvitation.objects.select_related("organization", "inviter")
            .filter(token=request.GET.get("token", ""))
            .first()
        )
        if self.invitation is None or not self.invitation.is_usable:
            return self.render({"state": "invalid"}, status=400)
        return super().dispatch(request, *args, **kwargs)

    def render(self, context, status=200):
        context = {"invitation": self.invitation, "role_label": self._role_label()} | context
        return render(self.request, self.template_name, context, status=status)

    def _role_label(self):
        if self.invitation is None:
            return ""
        return OrganizationRole(self.invitation.role).label

    def _state(self, request):
        if request.user.is_authenticated:
            if request.user.email.lower() != self.invitation.email.lower():
                return "wrong_account"
            return "confirm"
        if User.objects.filter(email__iexact=self.invitation.email).exists():
            return "sign_in"
        return "sign_up"

    def get(self, request):
        state = self._state(request)
        context = {"state": state, "login_url": self._login_url(request)}
        if state == "sign_up":
            context["form"] = InvitedSignupForm(invitation=self.invitation)
        return self.render(context)

    def post(self, request):
        state = self._state(request)
        if state == "confirm":
            try:
                membership = accept_invitation(invitation=self.invitation, user=request.user)
            except ValueError as error:
                return self.render({"state": "error", "error": str(error)}, status=400)
            return self._joined(membership)
        if state == "sign_up":
            form = InvitedSignupForm(request.POST, invitation=self.invitation)
            if not form.is_valid():
                return self.render({"state": state, "form": form}, status=400)
            try:
                membership = register_and_accept_invitation(
                    invitation=self.invitation,
                    first_name=form.cleaned_data["first_name"],
                    last_name=form.cleaned_data["last_name"],
                    password=form.cleaned_data["new_password1"],
                )
            except IntegrityError:  # an account was created for this email meanwhile
                return self.render(
                    {"state": "sign_in", "login_url": self._login_url(request)}, status=400
                )
            except ValueError as error:
                return self.render({"state": "error", "error": str(error)}, status=400)
            login(request, membership.user, backend="django.contrib.auth.backends.ModelBackend")
            return self._joined(membership)
        return self.render({"state": state, "login_url": self._login_url(request)}, status=400)

    def _joined(self, membership):
        set_active_organization(self.request, membership.organization)
        messages.success(self.request, f"Welcome to {membership.organization.name}.")
        return redirect("home")

    @staticmethod
    def _login_url(request):
        return f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}"


class SwitchOrganizationView(View):
    """POST ``/app/switch/<slug>/``: choose among the user's own active memberships."""

    def post(self, request, slug):
        if not request.user.is_authenticated:
            return redirect("login")
        membership = (
            OrganizationMembership.objects.select_related("organization")
            .filter(
                user=request.user,
                is_active=True,
                organization__is_active=True,
                organization__slug=slug,
            )
            .first()
        )
        if membership is None:
            messages.error(request, "You are not a member of that organization.")
            return redirect("home")
        if membership.organization.is_suspended:
            messages.error(request, f"{membership.organization.name} is suspended.")
            return redirect("home")
        set_active_organization(request, membership.organization)
        messages.success(request, f"Switched to {membership.organization.name}.")
        target = request.POST.get("next", "")
        if not url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            target = reverse("home")
        return redirect(target)


class TeamView(TenantPageMixin, HtmxPartialMixin, TemplateView):
    """Read-only team list: search, role filter and htmx pagination."""

    template_name = "organizations/team.html"
    required_capabilities = (Capability.MEMBERS_VIEW,)
    page_size = 25

    def get_context_data(self, **kwargs):
        query = self.request.GET.get("q", "").strip()
        role = self.request.GET.get("role", "")
        members = (
            OrganizationMembership.objects.filter(organization=self.tenant.organization)
            .select_related("user")
            .order_by("-is_active", "user__first_name", "user__last_name", "user__email")
        )
        if query:
            members = members.filter(
                Q(user__first_name__icontains=query)
                | Q(user__last_name__icontains=query)
                | Q(user__email__icontains=query)
                | Q(title__icontains=query)
            )
        if role in OrganizationRole.values:
            members = members.filter(role=role)
        page = Paginator(members, self.page_size).get_page(self.request.GET.get("page"))
        return super().get_context_data(**kwargs) | {
            "page_obj": page,
            "query": query,
            "role": role,
            "filters": [
                {
                    "name": "role",
                    "label": "Role",
                    "value": role,
                    "options": OrganizationRole.choices,
                    "all_label": "All roles",
                }
            ],
            "filtered": bool(query or role),
        }
