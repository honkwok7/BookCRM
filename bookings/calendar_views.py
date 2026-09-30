"""Calendar screens (M4.4): /app/calendar/ (day, week, month), the appointment panel and its
status actions, and a JSON feed of events.

Who sees what is ``bookings.calendar.calendar_bookings``: the whole organization with
``appointments.view_all``, otherwise (providers) only their own appointments. The page, the
panel and the feed all use it, so an appointment that isn't on your calendar is a 404 in the
panel too. Status changes go through the booking service and need ``appointments.manage``.

htmx: filters and navigation swap ``#calendar``; clicking an appointment loads its panel into
``#calendar-panel``; an action re-renders the panel and asks the calendar to refresh
(``HX-Trigger: calendar-refresh``). Without JavaScript every link and form is an ordinary page.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from bookings.calendar import (
    MAX_RANGE_DAYS,
    calendar_bookings,
    can_use_calendar,
    day_grid,
    local_bounds,
    month_grid,
    month_range,
    week_grid,
)
from bookings.models import Booking
from bookings.services import (
    ALLOWED_TRANSITIONS,
    RESCHEDULABLE_STATUSES,
    cancel_booking,
    change_booking_status,
)
from core.exceptions import DomainError
from core.web import TenantPageMixin, is_htmx, organization_zone
from crm.permissions import can_browse_customers
from locations.selectors import locations_for
from organizations.models import OrganizationRole
from organizations.permissions import Capability
from scheduling.availability import zone_of
from services.selectors import services_for
from staff.selectors import is_provider_here, own_profile, staff_for

VIEWS = ("day", "week", "month")
# Actions offered in the panel: (action, target status, button label).
ACTIONS = (
    ("confirm", Booking.Status.CONFIRMED, "Confirm"),
    ("check_in", Booking.Status.CHECKED_IN, "Check in"),
    ("start", Booking.Status.IN_PROGRESS, "Start"),
    ("check_out", Booking.Status.COMPLETED, "Check out"),
    ("no_show", Booking.Status.NO_SHOW, "No-show"),
    ("reject", Booking.Status.REJECTED, "Reject"),
)
ACTION_STATUS = {action: status for action, status, _ in ACTIONS}
STATUS_LEVEL = {
    Booking.Status.PENDING: "warning",
    Booking.Status.CONFIRMED: "brand",
    Booking.Status.CHECKED_IN: "success",
    Booking.Status.IN_PROGRESS: "success",
    Booking.Status.COMPLETED: "neutral",
    Booking.Status.CANCELLED: "danger",
    Booking.Status.NO_SHOW: "danger",
    Booking.Status.REJECTED: "danger",
}
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def change_source(tenant) -> str:
    if tenant.role == OrganizationRole.RECEPTIONIST:
        return Booking.Source.RECEPTION
    return Booking.Source.STAFF


class CalendarAccessMixin(TenantPageMixin):
    # The provider area's calendar (/staff/calendar/) shows only the member's own appointments,
    # whatever else they may see.
    own_only = False
    url_name = "app-calendar"

    def has_page_access(self, tenant) -> bool:
        if self.own_only:
            return is_provider_here(tenant)
        return can_use_calendar(tenant)

    def filters(self):
        """The chosen location, provider and service (ids from another organization are
        ignored), and the statuses to show."""
        params, organization = self.request.GET, self.tenant.organization

        def pick(queryset, key):
            value = params.get(key) or ""
            try:
                return queryset.filter(pk=value).first() if value else None
            except ValidationError:  # not a UUID
                return None

        location = pick(locations_for(organization), "location")
        staff = pick(self.visible_staff(), "staff")
        if self.own_only:
            staff = own_profile(self.tenant)
        service = pick(services_for(organization), "service")
        statuses = [value for value in params.getlist("status") if value in Booking.Status.values]
        return location, staff, service, statuses

    def visible_staff(self):
        staff = staff_for(self.tenant.organization).filter(is_active=True)
        if self.own_only or not self.tenant.has(Capability.APPOINTMENTS_VIEW_ALL):
            staff = staff.filter(user=self.tenant.user)
        return staff

    def zone_for(self, location):
        if location is not None:
            return zone_of(location)
        return organization_zone(self.tenant.organization)


class CalendarView(CalendarAccessMixin, View):
    template_name = "calendar/calendar.html"

    def get(self, request):
        location, staff, service, statuses = self.filters()
        zone = self.zone_for(location)
        today = timezone.now().astimezone(zone).date()
        view = request.GET.get("view") if request.GET.get("view") in VIEWS else "week"
        anchor = parse_date(request.GET.get("date") or "") or today

        context = {
            "organization": self.tenant.organization,
            "view": view,
            "anchor": anchor,
            "today": today,
            "zone": str(zone),
            "location": location,
            "staff": staff,
            "service": service,
            "statuses": statuses,
            "locations": locations_for(self.tenant.organization, active_only=True),
            "staff_options": self.visible_staff(),
            "service_options": services_for(self.tenant.organization).filter(is_archived=False),
            "status_options": Booking.Status.choices,
            "sees_everyone": not self.own_only
            and self.tenant.has(Capability.APPOINTMENTS_VIEW_ALL),
            "calendar_url": reverse(self.url_name),
            "page_heading": "My calendar" if self.own_only else "Calendar",
            "weekday_names": WEEKDAY_NAMES,
        }
        filters = dict(location=location, staff=staff, service=service, statuses=statuses)

        if view == "day":
            start, end = local_bounds(zone, anchor, 1)
            providers = self.visible_staff()
            if location is not None:
                providers = providers.filter(locations=location)
            if staff is not None:
                providers = providers.filter(pk=staff.pk)
            bookings = calendar_bookings(self.tenant, start, end, **filters)
            context["grid"] = day_grid(bookings, zone, anchor, list(providers))
            step = timedelta(days=1)
            context["title"] = f"{anchor:%A, %B} {anchor.day}, {anchor.year}"
            previous, following = anchor - step, anchor + step
        elif view == "week":
            first_day = anchor - timedelta(days=anchor.weekday())
            start, end = local_bounds(zone, first_day, 7)
            bookings = calendar_bookings(self.tenant, start, end, **filters)
            context["grid"] = week_grid(bookings, zone, first_day, today)
            last_day = first_day + timedelta(days=6)
            context["title"] = (
                f"{first_day:%b} {first_day.day} – {last_day:%b} {last_day.day}, {last_day.year}"
            )
            previous, following = anchor - timedelta(days=7), anchor + timedelta(days=7)
        else:
            first_day, after = month_range(anchor.year, anchor.month)
            start, end = local_bounds(zone, first_day, (after - first_day).days)
            bookings = calendar_bookings(self.tenant, start, end, **filters)
            context["weeks"] = month_grid(bookings, zone, anchor.year, anchor.month, today)
            context["title"] = f"{anchor:%B %Y}"
            previous = (anchor.replace(day=1) - timedelta(days=1)).replace(day=1)
            following = (anchor.replace(day=28) + timedelta(days=4)).replace(day=1)

        context["urls"] = {
            "previous": self.url(view=view, date=previous),
            "next": self.url(view=view, date=following),
            "today": self.url(view=view, date=today),
            "day": self.url(view="day", date=anchor),
            "week": self.url(view="week", date=anchor),
            "month": self.url(view="month", date=anchor),
        }
        context["day_url"] = self.url(view="day", date=None)
        return render(request, self.template_name, context)

    def url(self, **overrides) -> str:
        params = self.request.GET.copy()
        for key, value in overrides.items():
            params.pop(key, None)
            if value is not None:
                params[key] = value.isoformat() if isinstance(value, date) else value
        return f"{reverse(self.url_name)}?{params.urlencode()}"


class AppointmentMixin(CalendarAccessMixin):
    def get_booking(self, pk) -> Booking:
        """The appointment, if it would be on this member's calendar (else 404)."""
        bookings = Booking.objects.filter(organization=self.tenant.organization, pk=pk)
        if not self.tenant.has(Capability.APPOINTMENTS_VIEW_ALL):
            bookings = bookings.filter(staff__user=self.tenant.user)
        booking = bookings.select_related("service", "staff__user", "customer", "location").first()
        if booking is None:
            raise Http404("Appointment not found")
        return booking

    def render_panel(self, booking, *, error="", status=200):
        tenant = self.tenant
        can_manage = tenant.has(Capability.APPOINTMENTS_MANAGE)
        allowed = ALLOWED_TRANSITIONS[booking.status]
        location = booking.location
        zone = self.zone_for(location)
        context = {
            "organization": tenant.organization,
            "booking": booking,
            "zone": str(zone),
            "start": booking.start_datetime.astimezone(zone),
            "end": booking.end_datetime.astimezone(zone),
            "actions": [
                (action, label)
                for action, target, label in ACTIONS
                if can_manage and target in allowed
            ],
            "can_cancel": can_manage and Booking.Status.CANCELLED in allowed,
            "can_reschedule": can_manage and booking.status in RESCHEDULABLE_STATUSES,
            "show_internal_notes": tenant.has(Capability.CUSTOMERS_NOTES_PRIVATE),
            "customer_link": booking.customer_id and can_browse_customers(tenant),
            "history": booking.status_history.select_related("changed_by").order_by("created_at"),
            "status_level": STATUS_LEVEL.get(booking.status, "neutral"),
            "error": error,
            "in_panel": is_htmx(self.request),
        }
        template = "calendar/appointment.html"
        if is_htmx(self.request):
            template += "#panel"
        return render(self.request, template, context, status=status)


class AppointmentView(AppointmentMixin, View):
    def get(self, request, pk):
        return self.render_panel(self.get_booking(pk))


class AppointmentActionView(AppointmentMixin, View):
    """POST ``action`` (check_in, check_out, no_show, cancel, ...) and an optional ``reason``.

    Without htmx the answer is a redirect: to ``next`` when it is a page of this app (the
    reception dashboard sends one), otherwise to the appointment.
    """

    required_capabilities = (Capability.APPOINTMENTS_MANAGE,)

    @staticmethod
    def back_to(request) -> str | None:
        target = request.POST.get("next", "")
        safe = url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
        return target if safe and target.startswith(("/app/", "/staff/")) else None

    def post(self, request, pk):
        booking = self.get_booking(pk)
        action = request.POST.get("action", "")
        reason = request.POST.get("reason", "").strip()[:255]
        source = change_source(self.tenant)
        try:
            if action == "cancel":
                booking = cancel_booking(
                    booking=booking, actor=request.user, reason=reason, source=source
                )
            elif action in ACTION_STATUS:
                booking = change_booking_status(
                    booking=booking,
                    new_status=ACTION_STATUS[action],
                    actor=request.user,
                    reason=reason,
                    source=source,
                )
            else:
                raise DomainError("Unknown action", code="invalid_action")
        except DomainError as error:
            if not is_htmx(request):
                messages.error(request, error.message)
                return redirect(self.back_to(request) or reverse("app-appointment", args=[pk]))
            return self.render_panel(self.get_booking(pk), error=error.message, status=422)
        booking = self.get_booking(pk)
        if not is_htmx(request):
            messages.success(request, f"Appointment {booking.get_status_display().lower()}.")
            return redirect(self.back_to(request) or reverse("app-appointment", args=[pk]))
        response = self.render_panel(booking)
        response["HX-Trigger"] = "calendar-refresh"
        return response


class StaffCalendarView(CalendarView):
    """``/staff/calendar/``: the provider's own calendar (M5.3)."""

    own_only = True
    url_name = "staff-calendar"


class CalendarEventsView(CalendarAccessMixin, View):
    """``/app/calendar/events.json?start=YYYY-MM-DD&end=YYYY-MM-DD`` (end exclusive, at most
    62 days) with the same filters as the page. Minimal fields; never internal notes."""

    def get(self, request):
        location, staff, service, statuses = self.filters()
        zone = self.zone_for(location)
        first = parse_date(request.GET.get("start") or "")
        last = parse_date(request.GET.get("end") or "")
        if first is None or last is None or not 0 < (last - first).days <= MAX_RANGE_DAYS:
            return JsonResponse(
                {"detail": f"Give start and end dates, at most {MAX_RANGE_DAYS} days apart."},
                status=400,
            )
        start, end = local_bounds(zone, first, (last - first).days)
        bookings = calendar_bookings(
            self.tenant,
            start,
            end,
            location=location,
            staff=staff,
            service=service,
            statuses=statuses,
        )
        events = [
            {
                "id": str(booking.pk),
                "title": f"{booking.customer_name} · {booking.service.name}",
                "start": booking.start_datetime.astimezone(zone).isoformat(),
                "end": booking.end_datetime.astimezone(zone).isoformat(),
                "status": booking.status,
                "staff": {"id": str(booking.staff_id), "name": booking.staff.public_name},
                "service": {"id": str(booking.service_id), "name": booking.service.name},
                "location": str(booking.location_id) if booking.location_id else None,
                "url": reverse("app-appointment", kwargs={"pk": booking.pk}),
            }
            for booking in bookings
        ]
        return JsonResponse({"timezone": str(zone), "events": events})
