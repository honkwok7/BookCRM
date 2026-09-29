"""Landing pages for each kind of user, and the role-based redirect after sign-in."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from bookings.models import Booking
from bookings.selectors import bookings_for_customer_account, bookings_visible_to
from core.web import PlatformAdminMixin, TenantPageMixin, organization_zone, tenant_for_page
from dashboard.selectors import organization_dashboard_summary
from organizations.models import Organization, OrganizationRole
from organizations.permissions import Capability

User = get_user_model()
INACTIVE_STATUSES = (Booking.Status.CANCELLED, Booking.Status.REJECTED)


@login_required
def home(request):
    """Where "home" is depends on who you are: platform, team, staff or customer."""
    if request.user.is_superuser:
        return redirect("saas-dashboard")
    tenant = tenant_for_page(request)
    if tenant is None or tenant.role == OrganizationRole.CUSTOMER:
        return redirect("portal-home")
    if tenant.role == OrganizationRole.STAFF:
        return redirect("staff-dashboard")
    return redirect("app-dashboard")


def day_bounds(zone: ZoneInfo, day):
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start, start + timedelta(days=1)


class AppDashboardView(TenantPageMixin, TemplateView):
    template_name = "dashboard/app_dashboard.html"

    def get_context_data(self, **kwargs):
        organization = self.tenant.organization
        zone = organization_zone(organization)
        today = timezone.now().astimezone(zone).date()
        start, end = day_bounds(zone, today)
        appointments = (
            bookings_visible_to(self.request)
            .filter(start_datetime__gte=start, start_datetime__lt=end)
            .exclude(status__in=INACTIVE_STATUSES)
            .select_related("staff__user")
            .order_by("start_datetime")
        )
        summary = None
        if self.tenant.has(Capability.REPORTS_VIEW):
            summary = organization_dashboard_summary(organization)
        return super().get_context_data(**kwargs) | {
            "today": today,
            "zone": zone,
            "appointments": appointments[:50],
            "summary": summary,
            "sees_all_appointments": self.tenant.has(Capability.APPOINTMENTS_VIEW_ALL),
        }


class StaffDashboardView(TenantPageMixin, TemplateView):
    """The signed-in provider's own appointments for the next seven days."""

    template_name = "dashboard/staff_dashboard.html"
    days = 7

    def get_context_data(self, **kwargs):
        organization = self.tenant.organization
        zone = organization_zone(organization)
        today = timezone.now().astimezone(zone).date()
        start, _ = day_bounds(zone, today)
        appointments = (
            Booking.objects.filter(
                organization=organization,
                staff__user=self.request.user,
                start_datetime__gte=start,
                start_datetime__lt=start + timedelta(days=self.days),
            )
            .exclude(status__in=INACTIVE_STATUSES)
            .select_related("service")
            .order_by("start_datetime")
        )
        has_profile = organization.staff_profiles.filter(user=self.request.user).exists()
        return super().get_context_data(**kwargs) | {
            "appointments": appointments,
            "zone": zone,
            "has_profile": has_profile,
            "days": self.days,
        }


class PortalHomeView(LoginRequiredMixin, TemplateView):
    """Customer portal. Lists the account's own upcoming bookings in every organization."""

    template_name = "dashboard/portal_home.html"

    def get_context_data(self, **kwargs):
        upcoming = (
            bookings_for_customer_account(self.request.user)
            .filter(start_datetime__gte=timezone.now())
            .exclude(status__in=INACTIVE_STATUSES)
            .order_by("start_datetime")[:20]
        )
        return super().get_context_data(**kwargs) | {"upcoming": upcoming}


class SaasDashboardView(PlatformAdminMixin, TemplateView):
    """Platform operator overview. Counts only: no tenant records are shown here."""

    template_name = "dashboard/saas_dashboard.html"

    def get_context_data(self, **kwargs):
        organizations = Organization.objects.all()
        return super().get_context_data(**kwargs) | {
            "stats": {
                "organizations": organizations.count(),
                "active": organizations.filter(is_active=True, is_suspended=False).count(),
                "suspended": organizations.filter(is_suspended=True).count(),
                "users": User.objects.filter(is_active=True).count(),
            }
        }


class ComponentGalleryView(TenantPageMixin, TemplateView):
    """Every UI component on one page, for development (DEBUG only)."""

    template_name = "dashboard/components.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.DEBUG:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_template_names(self):
        if self.request.GET.get("modal"):
            return [f"{self.template_name}#modal_demo"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        here = self.request.path
        return super().get_context_data(**kwargs) | {
            "demo_menu": [{"label": "Edit", "url": "#"}, {"label": "Duplicate", "url": "#"}],
            "demo_tabs": [
                {"label": "Overview", "url": here, "active": True},
                {"label": "Activity", "url": f"{here}#activity", "active": False},
            ],
            "demo_filters": [
                {
                    "name": "status",
                    "label": "Status",
                    "value": "",
                    "options": [("active", "Active"), ("archived", "Archived")],
                    "all_label": "All statuses",
                }
            ],
            "demo_page": Paginator(range(1, 101), 10).get_page(self.request.GET.get("page")),
        }
