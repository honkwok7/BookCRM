"""The provider area (M5.3): ``/staff/``.

- ``/staff/dashboard/``: the provider's day ("Good morning, Maya"), with status actions.
- ``/staff/calendar/`` and ``/staff/customers/`` reuse the calendar and CRM pages, narrowed to
  the signed-in provider (bookings.calendar_views.StaffCalendarView,
  crm.web_views.ProviderCustomerListView).
- ``/staff/availability/``: their weekly hours (read-only; managers set them), their time off,
  a request form (pending until approved) and the block-time quick action.

Every page is scoped to the member's own staff profile, whatever their role, so an owner who
also takes appointments sees only their own here. Members without a profile get the dashboard's
empty state; the other pages refuse them.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from bookings.calendar_views import STATUS_LEVEL
from bookings.models import Booking
from bookings.services import ALLOWED_TRANSITIONS
from core.web import TenantPageMixin, organization_zone
from scheduling.models import TimeOff
from scheduling.selectors import weekly_hours
from scheduling.services import block_time, can_cancel, cancel_time_off, request_time_off
from staff.forms import BlockTimeForm, TimeOffRequestForm
from staff.provider import provider_day
from staff.selectors import can_open_provider_day, is_provider_here, own_profile

# The status changes a provider makes from their day: (action, target status, label).
ROW_ACTIONS = (
    ("check_in", Booking.Status.CHECKED_IN, "Check in"),
    ("start", Booking.Status.IN_PROGRESS, "Start"),
    ("check_out", Booking.Status.COMPLETED, "Check out"),
    ("no_show", Booking.Status.NO_SHOW, "No-show"),
)
HISTORY = 10


class ProviderPageMixin(TenantPageMixin):
    def has_page_access(self, tenant) -> bool:
        return is_provider_here(tenant)

    @property
    def profile(self):
        return own_profile(self.tenant)


class ProviderDashboardView(TenantPageMixin, TemplateView):
    template_name = "staff/provider_dashboard.html"

    def has_page_access(self, tenant) -> bool:
        return can_open_provider_day(tenant)

    def get_context_data(self, **kwargs):
        profile = own_profile(self.tenant)
        context = {"profile": profile, "day": None}
        if profile is not None:
            day = provider_day(profile)
            for booking in day["today_rows"]:
                allowed = ALLOWED_TRANSITIONS[booking.status]
                booking.row_actions = [
                    (action, label) for action, target, label in ROW_ACTIONS if target in allowed
                ]
                booking.status_level = STATUS_LEVEL.get(booking.status, "neutral")
            context["day"] = day
        return super().get_context_data(**kwargs) | context


class ProviderAvailabilityView(ProviderPageMixin, View):
    template_name = "staff/provider_availability.html"

    def forms(self, data=None, which=None):
        zone = organization_zone(self.tenant.organization)
        return {
            "block_form": BlockTimeForm(data if which == "block" else None, zone=zone),
            "request_form": TimeOffRequestForm(data if which == "request" else None, zone=zone),
        }

    def render_page(self, forms, *, status=200):
        profile = self.profile
        entries = list(TimeOff.objects.filter(staff=profile).order_by("-start_datetime")[:HISTORY])
        for entry in entries:
            entry.cancellable = can_cancel(entry)
        context = {
            "organization": self.tenant.organization,
            "profile": profile,
            "zone": organization_zone(self.tenant.organization),
            "entries": entries,
            **weekly_hours(profile),
            **forms,
        }
        return render(self.request, self.template_name, context, status=status)

    def get(self, request):
        return self.render_page(self.forms())

    def post(self, request):
        which = request.POST.get("form")
        if which not in ("block", "request"):
            return redirect("staff-availability")
        forms = self.forms(request.POST, which)
        form = forms[f"{which}_form"]
        if form.is_valid():
            start, end = form.period()
            write = block_time if which == "block" else request_time_off
            try:
                write(
                    staff=self.profile,
                    start=start,
                    end=end,
                    reason=form.cleaned_data["reason"],
                    actor=request.user,
                )
            except ValidationError as error:
                form.add_error(None, " ".join(error.messages))
            else:
                messages.success(
                    request,
                    "Time blocked." if which == "block" else "Time off requested.",
                )
                return redirect("staff-availability")
        return self.render_page(forms, status=400)


class ProviderTimeOffCancelView(ProviderPageMixin, View):
    def post(self, request, pk):
        # Only the provider's own entries: anyone else's is a 404.
        entry = TimeOff.objects.filter(pk=pk, staff=self.profile).first()
        if entry is None:
            raise Http404("Time off not found")
        try:
            cancel_time_off(entry=entry, actor=request.user)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            messages.success(request, "Time off cancelled.")
        return redirect(reverse("staff-availability"))
