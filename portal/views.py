"""The customer portal (M5.4): ``/portal/``.

- ``/portal/``: every business where the account is a customer, with what is coming up.
- ``/portal/<slug>/``: one business: the next appointment, what is coming up, recent visits and
  a link to book (the public booking wizard, which links the booking to the account).
- ``/portal/<slug>/appointments/``: upcoming and past; each appointment can be cancelled or
  moved while the service's cancellation deadline allows.
- ``/portal/<slug>/profile/``: name, phone and communication preferences.

Every page below ``/portal/<slug>/`` is scoped to that one business (``portal.selectors``);
a business, or an appointment, the account has nothing to do with is a 404. Nothing internal
is ever shown (internal notes, alerts, tags).
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views import View
from django.views.generic import TemplateView

from bookings.models import Booking
from bookings.services import cancel_booking, reschedule_booking
from core.exceptions import ConflictError, DomainError
from crm.services import update_customer
from portal.forms import CancelForm, ProfileForm
from portal.policy import change_policy, local_time, localize, zone_for
from portal.selectors import (
    portal_bookings,
    portal_customer,
    portal_organization,
    portal_organizations,
)
from scheduling.availability import AvailabilityService

UPCOMING = 10
RECENT = 5
HISTORY_PAGE = 20
# What the portal still lists as "upcoming" (a cancelled or declined one moves to history).
ON = (
    Booking.Status.PENDING,
    Booking.Status.CONFIRMED,
    Booking.Status.CHECKED_IN,
    Booking.Status.IN_PROGRESS,
)
SOURCE = Booking.Source.CUSTOMER_PORTAL


class PortalHomeView(LoginRequiredMixin, TemplateView):
    template_name = "portal/home.html"

    def get_context_data(self, **kwargs):
        user = self.request.user
        now = timezone.now()
        places = []
        for organization in portal_organizations(user):
            upcoming = localize(
                list(
                    portal_bookings(user, organization)
                    .filter(status__in=ON, end_datetime__gt=now)
                    .order_by("start_datetime")[:3]
                )
            )
            places.append({"organization": organization, "upcoming": upcoming})
        return super().get_context_data(**kwargs) | {"places": places}


class PortalOrganizationMixin(LoginRequiredMixin):
    section = ""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.organization = portal_organization(request.user, kwargs["slug"])
        if self.organization is None:
            raise Http404("Not found")
        return super().dispatch(request, *args, **kwargs)

    def bookings(self):
        return portal_bookings(self.request.user, self.organization)

    def get_booking(self, pk) -> Booking:
        booking = self.bookings().filter(pk=pk).first()
        if booking is None:
            raise Http404("Appointment not found")
        return booking

    def tabs(self):
        slug = self.organization.slug
        tabs = [
            ("overview", "Overview", reverse("portal-organization", args=[slug])),
            ("appointments", "Appointments", reverse("portal-appointments", args=[slug])),
        ]
        if self.organization.booking_page_enabled:
            tabs.append(("book", "Book", reverse("public-booking", args=[slug])))
        tabs.append(("profile", "My details", reverse("portal-profile", args=[slug])))
        return [
            {"label": label, "url": url, "active": key == self.section} for key, label, url in tabs
        ]

    def page(self, template, context, *, status=200):
        return render(
            self.request,
            template,
            {"organization": self.organization, "tabs": self.tabs(), **context},
            status=status,
        )


class PortalOrganizationView(PortalOrganizationMixin, View):
    section = "overview"

    def get(self, request, slug):
        now = timezone.now()
        upcoming = localize(
            list(
                self.bookings()
                .filter(status__in=ON, end_datetime__gt=now)
                .order_by("start_datetime")[:UPCOMING]
            )
        )
        recent = localize(
            list(
                self.bookings()
                .filter(status=Booking.Status.COMPLETED)
                .order_by("-start_datetime")[:RECENT]
            )
        )
        return self.page(
            "portal/organization.html",
            {
                "next": upcoming[0] if upcoming else None,
                "upcoming": upcoming[1:],
                "recent": recent,
                "can_book": self.organization.booking_page_enabled,
            },
        )


class PortalAppointmentsView(PortalOrganizationMixin, View):
    section = "appointments"

    def get(self, request, slug):
        now = timezone.now()
        bookings = self.bookings()
        upcoming = localize(
            list(bookings.filter(status__in=ON, end_datetime__gt=now).order_by("start_datetime"))
        )
        past = bookings.exclude(pk__in=[b.pk for b in upcoming]).order_by("-start_datetime")
        page = Paginator(past, HISTORY_PAGE).get_page(request.GET.get("page"))
        localize(page.object_list)
        for booking in upcoming:
            booking.policy = change_policy(booking, now=now)
        return self.page("portal/appointments.html", {"upcoming": upcoming, "page_obj": page})


class PortalAppointmentView(PortalOrganizationMixin, View):
    section = "appointments"

    def get(self, request, slug, pk):
        booking = localize([self.get_booking(pk)])[0]
        return self.page(
            "portal/appointment.html",
            {"booking": booking, "policy": change_policy(booking)},
        )


class PortalCancelView(PortalOrganizationMixin, View):
    section = "appointments"
    template_name = "portal/cancel.html"

    def render_form(self, booking, form, *, status=200, error=""):
        return self.page(
            self.template_name,
            {"booking": localize([booking])[0], "form": form, "error": error},
            status=status,
        )

    def get(self, request, slug, pk):
        booking = self.get_booking(pk)
        if not change_policy(booking).can_cancel:
            return redirect("portal-appointment", slug=slug, pk=pk)
        return self.render_form(booking, CancelForm())

    def post(self, request, slug, pk):
        booking = self.get_booking(pk)
        form = CancelForm(request.POST)
        if not form.is_valid():
            return self.render_form(booking, form, status=422)
        try:
            cancel_booking(
                booking=booking,
                actor=request.user,
                reason=form.cleaned_data["reason"],
                enforce_deadline=True,
                source=SOURCE,
            )
        except (DomainError, ConflictError) as error:
            return self.render_form(booking, form, status=422, error=error.message)
        messages.success(request, "Your appointment is cancelled.")
        return redirect("portal-appointment", slug=slug, pk=pk)


class PortalRescheduleView(PortalOrganizationMixin, View):
    """Pick another time with the same provider at the same place: one day at a time, the
    times the public booking page would offer (the appointment itself doesn't block them)."""

    section = "appointments"
    template_name = "portal/reschedule.html"

    def render_day(self, booking, day, *, status=200, error=""):
        zone = zone_for(booking)
        today = timezone.now().astimezone(zone).date()
        # Start on the appointment's own day: moving it by an hour is the common case.
        day = max(day or booking.start_datetime.astimezone(zone).date(), today)
        engine = AvailabilityService(
            self.organization, booking.service, location=booking.location, public=True
        )
        slots = engine.get_available_slots(day, day, staff=booking.staff, ignore_booking=booking)
        times = [
            {"value": slot.start.isoformat(), "label": local_time(slot.start, zone)}
            for slot in slots
            if slot.start != booking.start_datetime
        ]
        base = reverse("portal-reschedule", args=[self.organization.slug, booking.pk])
        return self.page(
            self.template_name,
            {
                "booking": localize([booking])[0],
                "day": day,
                "times": times,
                "previous_url": f"{base}?date={day - timedelta(days=1)}" if day > today else "",
                "next_url": f"{base}?date={day + timedelta(days=1)}",
                "error": error,
            },
            status=status,
        )

    def get(self, request, slug, pk):
        booking = self.get_booking(pk)
        if not change_policy(booking).can_reschedule:
            return redirect("portal-appointment", slug=slug, pk=pk)
        return self.render_day(booking, parse_date(request.GET.get("date") or ""))

    def post(self, request, slug, pk):
        booking = self.get_booking(pk)
        new_start = parse_datetime(request.POST.get("start") or "")
        day = parse_date(request.POST.get("date") or "")
        if new_start is None or timezone.is_naive(new_start):
            return self.render_day(booking, day, status=422, error="Please choose a time.")
        try:
            moved = reschedule_booking(
                booking=booking,
                new_start=new_start,
                actor=request.user,
                public=True,
                source=SOURCE,
            )
        except (DomainError, ConflictError) as error:
            return self.render_day(booking, day, status=422, error=error.message)
        messages.success(request, "Your appointment is moved.")
        return redirect("portal-appointment", slug=slug, pk=moved.pk)


class PortalProfileView(PortalOrganizationMixin, View):
    section = "profile"
    template_name = "portal/profile.html"

    def get(self, request, slug):
        customer = portal_customer(request.user, self.organization)
        form = ProfileForm(instance=customer) if customer else None
        return self.page(self.template_name, {"customer": customer, "form": form})

    def post(self, request, slug):
        customer = portal_customer(request.user, self.organization)
        if customer is None:
            raise Http404("Nothing to change yet")
        form = ProfileForm(request.POST, instance=customer)
        if form.is_valid():
            changes = {field: form.cleaned_data[field] for field in form.changed_data}
            try:
                if changes:
                    update_customer(customer=customer, actor=request.user, **changes)
            except (DomainError, ConflictError) as error:
                form.add_error(None, error.message)
            else:
                messages.success(request, "Your details are saved.")
                return redirect("portal-profile", slug=slug)
        return self.page(self.template_name, {"customer": customer, "form": form}, status=422)
