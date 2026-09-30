"""Reception and provider appointment screens (M4.5): the list, new appointment, walk-in and
reschedule. The appointment page and its status actions are in calendar_views.

Scoping is the calendar's: ``appointments.view_all`` sees and books for everyone; providers
only see their own appointments and book only for themselves. Booking needs
``appointments.manage`` and goes through the booking service with the team's rules and the
source ``reception`` (receptionists) or ``staff``.

htmx: the forms open in the page dialog; a change re-renders the form (``refresh``), a
submission books and redirects (HX-Redirect); errors answer 422. Without JavaScript the same
URLs are pages, and the form's "Update" button refreshes the choices.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views import View
from django.views.generic import TemplateView

from bookings.app_forms import ANYONE, AppointmentForm, RescheduleForm, next_minute
from bookings.calendar import local_bounds
from bookings.calendar_views import (
    STATUS_LEVEL,
    AppointmentMixin,
    CalendarAccessMixin,
    change_source,
)
from bookings.models import Booking
from bookings.services import (
    ACTIVE_BOOKING_STATUSES,
    RESCHEDULABLE_STATUSES,
    check_in,
    create_booking,
    reschedule_booking,
)
from core.exceptions import ConflictError, DomainError
from core.web import HtmxPartialMixin, is_htmx
from crm.selectors import customers_visible_to
from organizations.permissions import Capability
from staff.models import StaffProfile

WHEN = {
    "upcoming": "Upcoming",
    "today": "Today",
    "past": "Past",
    "all": "All dates",
}


def done(request, booking, message):
    """After booking: to the appointment's page (a dialog closes through HX-Redirect)."""
    messages.success(request, message)
    url = reverse("app-appointment", kwargs={"pk": booking.pk})
    if is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


class AppointmentListView(CalendarAccessMixin, HtmxPartialMixin, TemplateView):
    template_name = "appointments/list.html"
    page_size = 25

    def get_context_data(self, **kwargs):
        tenant = self.tenant
        params = self.request.GET
        location, staff, service, statuses = self.filters()
        when = params.get("when") if params.get("when") in WHEN else "upcoming"
        query = params.get("q", "").strip()

        bookings = Booking.objects.filter(organization=tenant.organization).select_related(
            "service", "staff__user", "location"
        )
        if not tenant.has(Capability.APPOINTMENTS_VIEW_ALL):
            bookings = bookings.filter(staff__user=tenant.user)
        now = timezone.now()
        zone = self.zone_for(location)
        today = now.astimezone(zone).date()
        if when == "upcoming":
            bookings = bookings.filter(end_datetime__gte=now).order_by("start_datetime")
        elif when == "today":
            start, end = local_bounds(zone, today, 1)
            bookings = bookings.filter(start_datetime__gte=start, start_datetime__lt=end)
            bookings = bookings.order_by("start_datetime")
        elif when == "past":
            bookings = bookings.filter(end_datetime__lt=now).order_by("-start_datetime")
        else:
            bookings = bookings.order_by("-start_datetime")
        if statuses:
            bookings = bookings.filter(status__in=statuses)
        elif when in ("upcoming", "today"):
            bookings = bookings.filter(status__in=ACTIVE_BOOKING_STATUSES)
        for field, value in (("location", location), ("staff", staff), ("service", service)):
            if value is not None:
                bookings = bookings.filter(**{field: value})
        if query:
            bookings = bookings.filter(
                Q(customer_name__icontains=query)
                | Q(customer_email__icontains=query)
                | Q(customer_phone__icontains=query)
                | Q(reference__iexact=query)
            )
        page = Paginator(bookings, self.page_size).get_page(params.get("page"))
        for booking in page:
            booking.status_level = STATUS_LEVEL.get(booking.status, "neutral")
        filters = [
            {
                "name": "when",
                "label": "When",
                "value": when,
                "options": [(key, label) for key, label in WHEN.items() if key != "upcoming"],
                "all_label": "Upcoming",
            },
            {
                "name": "status",
                "label": "Status",
                "value": statuses[0] if statuses else "",
                "options": Booking.Status.choices,
                "all_label": "Any status",
            },
        ]
        if tenant.has(Capability.APPOINTMENTS_VIEW_ALL):
            filters.append(
                {
                    "name": "staff",
                    "label": "Provider",
                    "value": str(staff.pk) if staff else "",
                    "options": [(str(item.pk), item.public_name) for item in self.visible_staff()],
                    "all_label": "Every provider",
                }
            )
        return super().get_context_data(**kwargs) | {
            "organization": tenant.organization,
            "page_obj": page,
            "query": query,
            "filters": filters,
            "filtered": bool(query or statuses or staff or when != "upcoming"),
            "can_book": tenant.has(Capability.APPOINTMENTS_MANAGE),
            "zone": str(zone),
        }


class BookingFormMixin(CalendarAccessMixin):
    required_capabilities = (Capability.APPOINTMENTS_MANAGE,)
    template_name = "appointments/form.html"
    walk_in = False

    def own_staff(self):
        """A provider without ``appointments.view_all`` books only for themselves."""
        if self.tenant.has(Capability.APPOINTMENTS_VIEW_ALL):
            return None
        profile = StaffProfile.objects.filter(
            organization=self.tenant.organization, user=self.tenant.user
        ).first()
        # Nobody to book for: the form offers no providers.
        return profile or StaffProfile(pk=None)

    def make_form(self, data=None):
        return AppointmentForm(
            data,
            tenant=self.tenant,
            customers=customers_visible_to(self.request),
            own_staff=self.own_staff(),
            walk_in=self.walk_in,
        )

    def render_form(self, form, *, status=200, error=""):
        context = {
            "organization": self.tenant.organization,
            "form": form,
            "walk_in": self.walk_in,
            "action": self.request.path,
            "in_modal": is_htmx(self.request),
            "error": error,
            "title": "Walk-in" if self.walk_in else "New appointment",
        }
        name = f"{self.template_name}#form" if is_htmx(self.request) else self.template_name
        return render(self.request, name, context, status=status)

    def get(self, request):
        return self.render_form(self.make_form())

    def post(self, request):
        form = self.make_form(request.POST)
        if "refresh" in request.POST:
            form.errors.clear()  # re-rendering the choices, not submitting
            return self.render_form(form)
        if not form.is_valid():
            return self.render_form(form, status=422)
        try:
            booking = self.book(form)
        except DomainError as error:
            return self.render_form(form, status=422, error=error.message)
        if booking is None:
            return self.render_form(
                form, status=422, error="That time was just taken. Choose another time."
            )
        return done(request, booking, self.success_message)


class NewAppointmentView(BookingFormMixin, View):
    success_message = "Appointment booked."

    def book(self, form):
        start = parse_datetime(form.cleaned_data["start"])
        for staff in form.staff_candidates(start):
            try:
                return create_booking(
                    organization=self.tenant.organization,
                    service=form.service,
                    staff_profile=staff,
                    location=form.location,
                    start_datetime=start,
                    source=change_source(self.tenant),
                    customer_timezone=str(form.zone),
                    customer_notes=form.cleaned_data["customer_notes"],
                    actor=self.request.user,
                    notify=True,
                    **form.customer_details(),
                )
            except ConflictError as error:
                # With "first available", someone else may still be free at that time.
                if form.staff_choice != ANYONE or error.code != "slot_unavailable":
                    raise
        return None


class WalkInView(BookingFormMixin, View):
    """Someone walks in: book from the next minute with a free provider, and check them in."""

    walk_in = True
    success_message = "Walk-in checked in."

    def book(self, form):
        start = next_minute(timezone.now())
        for staff in form.staff_candidates():
            try:
                with transaction.atomic():
                    booking = create_booking(
                        organization=self.tenant.organization,
                        service=form.service,
                        staff_profile=staff,
                        location=form.location,
                        start_datetime=start,
                        source=change_source(self.tenant),
                        customer_timezone=str(form.zone),
                        customer_notes=form.cleaned_data["customer_notes"],
                        actor=self.request.user,
                        notify=False,
                        **form.customer_details(),
                    )
                    return check_in(
                        booking=booking, actor=self.request.user, source=change_source(self.tenant)
                    )
            except ConflictError as error:
                if error.code != "slot_unavailable":
                    raise
        raise ConflictError("Nobody is free for this service right now.", code="slot_unavailable")


class RescheduleView(AppointmentMixin, View):
    required_capabilities = (Capability.APPOINTMENTS_MANAGE,)
    template_name = "appointments/reschedule.html"

    def render_form(self, booking, form, *, status=200, error=""):
        context = {
            "organization": self.tenant.organization,
            "booking": booking,
            "form": form,
            "action": self.request.path,
            "in_modal": is_htmx(self.request),
            "error": error,
        }
        name = f"{self.template_name}#form" if is_htmx(self.request) else self.template_name
        return render(self.request, name, context, status=status)

    def get(self, request, pk):
        booking = self.get_booking(pk)
        return self.render_form(booking, RescheduleForm(booking=booking))

    def post(self, request, pk):
        booking = self.get_booking(pk)
        form = RescheduleForm(request.POST, booking=booking)
        if booking.status not in RESCHEDULABLE_STATUSES:
            return self.render_form(
                booking, form, status=422, error="This appointment can't be rescheduled."
            )
        if "refresh" in request.POST:
            form.errors.clear()
            return self.render_form(booking, form)
        if not form.is_valid():
            return self.render_form(booking, form, status=422)
        try:
            moved = reschedule_booking(
                booking=booking,
                new_start=parse_datetime(form.cleaned_data["start"]),
                actor=request.user,
                source=change_source(self.tenant),
            )
        except DomainError as error:
            return self.render_form(booking, form, status=422, error=error.message)
        return done(request, moved, "Appointment rescheduled.")
