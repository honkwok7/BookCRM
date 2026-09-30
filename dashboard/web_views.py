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
from core.web import (
    HtmxPartialMixin,
    PlatformAdminMixin,
    TenantPageMixin,
    organization_zone,
    tenant_for_page,
)
from dashboard.reporting import PERIODS, Filters, dashboard_report, upcoming_appointments
from locations.models import Location
from organizations.models import Organization, OrganizationRole
from organizations.permissions import Capability
from staff.models import StaffProfile

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


class AppDashboardView(TenantPageMixin, HtmxPartialMixin, TemplateView):
    """The organization dashboard (M5.1).

    Everyone on the team sees the operational part: the upcoming appointments they may see.
    With ``reports.view`` (owners and managers) the analytics widgets are added, narrowed by
    period, location and provider; the filter bar refreshes ``#dashboard`` through htmx.
    """

    template_name = "dashboard/app_dashboard.html"
    partial_name = "dashboard"

    def _filters(self, organization):
        """The requested filters, keeping only values that belong to the organization."""
        query = self.request.GET
        days = int(query["days"]) if query.get("days", "").isdigit() else 30
        locations = list(
            Location.objects.filter(organization=organization, is_active=True).order_by(
                "-is_default", "name"
            )
        )
        providers = list(
            StaffProfile.objects.filter(organization=organization, is_active=True)
            .select_related("user")
            .order_by("display_name", "user__first_name", "user__last_name")
        )
        location = next((str(i.pk) for i in locations if str(i.pk) == query.get("location")), None)
        staff = next((str(i.pk) for i in providers if str(i.pk) == query.get("staff")), None)
        filters = Filters(days=days if days in PERIODS else 30, location=location, staff=staff)
        bar = [
            {
                "name": "days",
                "label": "Period",
                "value": str(filters.days),
                "options": [(str(n), f"Last {n} days") for n in PERIODS if n != 30],
                "all_label": "Last 30 days",
            },
        ]
        if len(locations) > 1:
            bar.append(
                {
                    "name": "location",
                    "label": "Location",
                    "value": location or "",
                    "options": [(str(i.pk), i.name) for i in locations],
                    "all_label": "Every location",
                }
            )
        bar.append(
            {
                "name": "staff",
                "label": "Provider",
                "value": staff or "",
                "options": [(str(i.pk), i.public_name) for i in providers],
                "all_label": "Every provider",
            }
        )
        return filters, bar

    def get_context_data(self, **kwargs):
        organization = self.tenant.organization
        zone = organization_zone(organization)
        now = timezone.now()
        visible = bookings_visible_to(self.request)
        context = {
            "today": now.astimezone(zone).date(),
            "zone": zone,
            "sees_all_appointments": self.tenant.has(Capability.APPOINTMENTS_VIEW_ALL),
            "report": None,
        }
        if self.tenant.has(Capability.REPORTS_VIEW):
            filters, bar = self._filters(organization)
            if filters.location:
                visible = visible.filter(location_id=filters.location)
            if filters.staff:
                visible = visible.filter(staff_id=filters.staff)
            report = dashboard_report(organization, filters, now=now)
            context |= {
                "report": report,
                "filters": filters,
                "filter_bar": bar,
                "filtered": bool(filters.location or filters.staff),
                "chart": volume_chart(report["volume"], report["volume_max"]),
                "currency": organization.currency,
            }
        context["upcoming"] = upcoming_appointments(visible, now=now)
        return super().get_context_data(**kwargs) | context


CHART_HEIGHT = 100
BAR_WIDTH = 10


def volume_chart(volume: list[dict], highest: int) -> dict:
    """Bars for an SVG chart (drawn with attributes, not styles: the CSP forbids inline
    styles). Heights are scaled to the busiest day."""
    bars = []
    for index, item in enumerate(volume):
        height = round(CHART_HEIGHT * item["count"] / highest, 1) if highest else 0
        bars.append(
            item
            | {
                "x": index * BAR_WIDTH + 1,
                "y": CHART_HEIGHT - height,
                "height": height,
                "width": BAR_WIDTH - 2,
            }
        )
    return {"bars": bars, "width": len(volume) * BAR_WIDTH, "height": CHART_HEIGHT}


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
