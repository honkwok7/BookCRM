"""The reception dashboard (M5.2): ``/app/reception/``.

One location at a time (the default, or ``?location=`` when there are several). The live part
(``#reception-live``: timeline, waiting, providers now, cancellations, waitlist) refreshes
itself every 30 seconds through htmx polling; the next free time per service loads separately
(``/app/reception/next-free/``) because it runs the availability engine.

Needs ``appointments.manage`` and ``appointments.view_all``: receptionists, managers and
owners. Actions (check in, check out, no-show) post to the appointment action view with
``next`` pointing back here.
"""

from __future__ import annotations

from django.utils import timezone
from django.views.generic import TemplateView

from bookings.calendar_views import STATUS_LEVEL
from bookings.models import Booking
from bookings.reception import front_desk, next_free_times
from bookings.services import ALLOWED_TRANSITIONS, RESCHEDULABLE_STATUSES
from core.web import HtmxPartialMixin, TenantPageMixin, is_htmx
from locations.models import Location
from organizations.permissions import Capability
from scheduling.availability import zone_of

# The quick status changes offered on each row: (action, target status, label).
ROW_ACTIONS = (
    ("check_in", Booking.Status.CHECKED_IN, "Check in"),
    ("check_out", Booking.Status.COMPLETED, "Check out"),
    ("no_show", Booking.Status.NO_SHOW, "No-show"),
)


class ReceptionMixin(TenantPageMixin):
    required_capabilities = (Capability.APPOINTMENTS_MANAGE, Capability.APPOINTMENTS_VIEW_ALL)

    def locations(self) -> list[Location]:
        return list(
            Location.objects.filter(organization=self.tenant.organization, is_active=True).order_by(
                "-is_default", "name"
            )
        )

    def location(self, locations) -> Location | None:
        wanted = self.request.GET.get("location", "")
        return next((item for item in locations if str(item.pk) == wanted), None) or next(
            (item for item in locations if item.is_default), locations[0] if locations else None
        )


class ReceptionView(ReceptionMixin, HtmxPartialMixin, TemplateView):
    template_name = "reception/dashboard.html"
    partial_name = "live"

    def get_context_data(self, **kwargs):
        organization = self.tenant.organization
        locations = self.locations()
        location = self.location(locations)
        context = {"organization": organization, "location": location, "desk": None}
        if location is not None:
            desk = front_desk(organization, location)
            for booking in desk["timeline"]:
                allowed = ALLOWED_TRANSITIONS[booking.status]
                booking.row_actions = [
                    (action, label) for action, target, label in ROW_ACTIONS if target in allowed
                ]
                booking.can_reschedule = booking.status in RESCHEDULABLE_STATUSES
                booking.status_level = STATUS_LEVEL.get(booking.status, "neutral")
            context["desk"] = desk
        context["filters"] = (
            [
                {
                    "name": "location",
                    "label": "Location",
                    "value": str(location.pk) if location else "",
                    "options": [
                        (str(item.pk), item.name) for item in locations if not item.is_default
                    ],
                    "all_label": next((item.name for item in locations if item.is_default), ""),
                }
            ]
            if len(locations) > 1
            else []
        )
        return super().get_context_data(**kwargs) | context


class NextFreeView(ReceptionMixin, TemplateView):
    """The next free time per service (loaded by the reception page, cached for a minute)."""

    def get_template_names(self):
        if is_htmx(self.request):
            return ["reception/next_free.html"]
        return ["reception/next_free_page.html"]

    def get_context_data(self, **kwargs):
        location = self.location(self.locations())
        rows = next_free_times(self.tenant.organization, location) if location else []
        today = timezone.now().astimezone(zone_of(location)).date() if location else None
        return super().get_context_data(**kwargs) | {
            "organization": self.tenant.organization,
            "rows": rows,
            "location": location,
            "today": today,
        }
