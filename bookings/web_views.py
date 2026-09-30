"""The public booking wizard: /book/<slug>/ (M4.6). State and re-validation: bookings/wizard.py.

Every step is a plain page with a plain form (POST, then redirect to the next step), so booking
works without JavaScript. With JavaScript, htmx boosts the links and forms of the wizard
(``hx-boost`` on its container) so steps swap in without a full page load. Form errors answer
422 (htmx swaps those in); a time that was taken meanwhile sends the visitor back to the time
step with an explanation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date, parse_datetime
from django.views import View

from bookings.forms import BookingDetailsForm
from bookings.models import Booking
from bookings.services import create_booking
from bookings.wizard import ANY_PROVIDER, STEPS, Wizard
from core import ratelimit
from core.audit import client_ip
from core.exceptions import ConflictError, DomainError
from organizations.branding import theme_css
from organizations.tenancy import get_public_organization
from scheduling.availability import AvailabilityService

STEP_URLS = {
    "location": "public-booking",
    "service": "public-booking-service",
    "provider": "public-booking-provider",
    "time": "public-booking-time",
    "details": "public-booking-details",
    "review": "public-booking-review",
}
STEP_LABELS = {
    "location": "Location",
    "service": "Service",
    "provider": "Provider",
    "time": "Date & time",
    "details": "Your details",
    "review": "Review",
}
DAYS_SHOWN = 7
TAKEN = "Sorry, that time was just booked by someone else. Please choose another time."


def step_url(organization, step: str) -> str:
    return reverse(STEP_URLS[step], kwargs={"slug": organization.slug})


def public_organization_or_404(slug):
    # Same rule as the public API: active, not suspended, booking page enabled.
    organization = get_public_organization(slug)
    if organization is None:
        raise Http404
    return organization


class WizardStepView(View):
    step: str = ""
    template_name = ""

    def dispatch(self, request, slug, *args, **kwargs):
        self.organization = public_organization_or_404(slug)
        self.wizard = Wizard.load(self.organization, request.session)
        if not self.wizard.reachable(self.step):
            return redirect(step_url(self.organization, self.wizard.first_open_step()))
        if request.method == "GET" and self.wizard.skipped(self.step):
            return redirect(self.next_url())
        return super().dispatch(request, slug, *args, **kwargs)

    def next_url(self) -> str:
        """The next step to show (skipping those answered for the visitor)."""
        for step in STEPS[STEPS.index(self.step) + 1 :]:
            if not self.wizard.skipped(step):
                return step_url(self.organization, step)
        return step_url(self.organization, "review")

    def back_url(self) -> str | None:
        for step in reversed(STEPS[: STEPS.index(self.step)]):
            if not self.wizard.skipped(step):
                return step_url(self.organization, step)
        return None

    def progress(self) -> list[dict]:
        shown = [step for step in STEPS if not self.wizard.skipped(step)]
        current = shown.index(self.step) if self.step in shown else 0
        return [
            {
                "label": STEP_LABELS[step],
                "url": step_url(self.organization, step),
                "state": "done" if index < current else "current" if index == current else "todo",
                "number": index + 1,
            }
            for index, step in enumerate(shown)
        ]

    def render_step(self, request, context=None, status=200):
        wizard = self.wizard
        context = {
            "organization": self.organization,
            "wizard": wizard,
            "step": self.step,
            "progress": self.progress(),
            "back_url": self.back_url(),
            "zone": wizard.location.timezone if wizard.location else self.organization.timezone,
            "sign_in_required": not self.organization.allow_guest_booking
            and not request.user.is_authenticated,
            **(context or {}),
        }
        return render(request, self.template_name, context, status=status)

    def invalid_choice(self, request, message):
        return self.render_step(request, {"error": message}, status=422)


class LocationStepView(WizardStepView):
    step = "location"
    template_name = "booking/step_location.html"

    def get(self, request, slug):
        return self.render_step(request, {"locations": self.wizard.locations()})

    def post(self, request, slug):
        chosen = request.POST.get("location", "")
        if chosen not in {str(item.pk) for item in self.wizard.locations()}:
            return self.invalid_choice(request, "Choose a location.")
        self.wizard.choose("location", chosen)
        return redirect(self.next_url())

    def render_step(self, request, context=None, status=200):
        context = {"locations": self.wizard.locations(), **(context or {})}
        return super().render_step(request, context, status)


class ServiceStepView(WizardStepView):
    step = "service"
    template_name = "booking/step_service.html"

    def get(self, request, slug):
        return self.render_step(request)

    def post(self, request, slug):
        chosen = request.POST.get("service", "")
        if chosen not in {str(item.pk) for item in self.wizard.services()}:
            return self.invalid_choice(request, "Choose a service.")
        self.wizard.choose("service", chosen)
        return redirect(self.next_url())

    def render_step(self, request, context=None, status=200):
        groups: dict = {}
        for service in self.wizard.services():
            groups.setdefault(service.category, []).append(service)
        context = {"groups": list(groups.items()), **(context or {})}
        return super().render_step(request, context, status)


class ProviderStepView(WizardStepView):
    step = "provider"
    template_name = "booking/step_provider.html"

    def get(self, request, slug):
        return self.render_step(request)

    def post(self, request, slug):
        chosen = request.POST.get("staff", "")
        valid = {str(item.pk) for item in self.wizard.providers()} | {ANY_PROVIDER}
        if chosen not in valid:
            return self.invalid_choice(request, "Choose who you'd like to see.")
        self.wizard.choose("provider", chosen)
        return redirect(self.next_url())

    def render_step(self, request, context=None, status=200):
        context = {"providers": self.wizard.providers(), **(context or {})}
        return super().render_step(request, context, status)


class TimeStepView(WizardStepView):
    """A week of days (with the days that have free times marked) and the free times on the
    selected day, grouped into morning, afternoon and evening."""

    step = "time"
    template_name = "booking/step_time.html"

    def engine(self) -> AvailabilityService:
        return AvailabilityService(
            self.organization, self.wizard.service, location=self.wizard.location, public=True
        )

    def get(self, request, slug):
        return self.render_step(request)

    def post(self, request, slug):
        start = parse_datetime(request.POST.get("start", ""))
        if start is None or start.tzinfo is None:
            return self.invalid_choice(request, "Choose a time.")
        try:
            self.validate(start)
        except DomainError as error:
            return self.render_step(request, {"error": error.message}, status=422)
        self.wizard.choose("time", start.isoformat())
        return redirect(self.next_url())

    def validate(self, start: datetime) -> None:
        """Raise unless the time is free with the chosen provider (or with someone)."""
        engine = self.engine()
        if self.wizard.staff is not None:
            engine.validate_slot(self.wizard.staff, start)
            return
        day = start.astimezone(engine.zone).date()
        if not any(slot.start == start for slot in engine.get_available_slots(day, day)):
            raise ConflictError(TAKEN, code="slot_unavailable")

    def render_step(self, request, context=None, status=200):
        engine = self.engine()
        today = engine.now.astimezone(engine.zone).date()
        last = today + timedelta(days=self.wizard.service.max_advance_days)
        first_day = parse_date(request.GET.get("from", "")) or today
        first_day = min(max(first_day, today), last)
        days = [first_day + timedelta(days=offset) for offset in range(DAYS_SHOWN)]
        days = [day for day in days if day <= last]
        slots = engine.get_available_slots(days[0], days[-1], staff=self.wizard.staff)
        by_day: dict[date, list] = {day: [] for day in days}
        for slot in slots:
            local = slot.start.astimezone(engine.zone)
            by_day.setdefault(local.date(), []).append(local)
        selected = parse_date(request.GET.get("date", ""))
        if selected not in by_day:
            selected = next((day for day in days if by_day[day]), days[0])
        periods = {"Morning": [], "Afternoon": [], "Evening": []}
        for moment in by_day.get(selected, []):
            label = (
                "Morning" if moment.hour < 12 else "Afternoon" if moment.hour < 17 else "Evening"
            )
            periods[label].append(moment)
        context = {
            "days": [{"date": day, "available": bool(by_day[day])} for day in days],
            "selected": selected,
            "periods": [(label, times) for label, times in periods.items() if times],
            "previous_from": (
                first_day - timedelta(days=DAYS_SHOWN) if first_day > today else None
            ),
            "next_from": (days[-1] + timedelta(days=1) if days[-1] < last else None),
            "notice": self.wizard.data.pop("notice", None),
            **(context or {}),
        }
        if context["notice"]:
            self.wizard._save()
        return super().render_step(request, context, status)


class DetailsStepView(WizardStepView):
    step = "details"
    template_name = "booking/step_details.html"

    def get(self, request, slug):
        initial = dict(self.wizard.customer)
        if not initial and request.user.is_authenticated:
            initial = {"name": request.user.get_full_name(), "email": request.user.email}
        return self.render_step(request, {"form": BookingDetailsForm(initial=initial)})

    def post(self, request, slug):
        form = BookingDetailsForm(request.POST)
        if not form.is_valid():
            return self.render_step(request, {"form": form}, status=422)
        self.wizard.choose("details", form.customer())
        return redirect(self.next_url())


class ReviewStepView(WizardStepView):
    step = "review"
    template_name = "booking/step_review.html"

    def get(self, request, slug):
        return self.render_step(request)

    def post(self, request, slug):
        if self.limited(request):
            return self.render_step(
                request,
                {"error": "Too many booking attempts. Please wait a while and try again."},
                status=429,
            )
        if not self.organization.allow_guest_booking and not request.user.is_authenticated:
            return self.render_step(request, status=403)
        wizard = self.wizard
        key = wizard.idempotency_key()
        # A second click on Confirm (or a retry) after the booking was made: show it.
        existing = Booking.objects.filter(organization=self.organization, idempotency_key=key)
        if existing.exists():
            return self.confirmed(existing.get())
        try:
            booking = self.book(request, key)
        except ConflictError:
            booking = None
        except DomainError as error:
            return self.render_step(request, {"error": error.message}, status=422)
        if booking is None:
            wizard.forget_time()
            wizard.data["notice"] = TAKEN
            wizard._save()
            return redirect(step_url(self.organization, "time"))
        return self.confirmed(booking)

    def confirmed(self, booking):
        self.wizard.clear()
        return redirect(
            "public-booking-confirmation",
            slug=self.organization.slug,
            public_uuid=booking.public_uuid,
        )

    def limited(self, request) -> bool:
        rate = settings.PUBLIC_BOOKING_RATE
        by_ip = ratelimit.hit(
            f"booking:{self.organization.pk}:ip", client_ip(request) or "unknown", rate
        )
        by_org = ratelimit.hit(
            "booking:org", str(self.organization.pk), settings.PUBLIC_BOOKING_ORG_RATE
        )
        return by_ip or by_org

    def candidates(self) -> list:
        """Who to book: the chosen provider, or everyone free at that time (in order)."""
        wizard = self.wizard
        if wizard.staff is not None:
            return [wizard.staff]
        engine = AvailabilityService(
            self.organization, wizard.service, location=wizard.location, public=True
        )
        day = wizard.start.astimezone(engine.zone).date()
        for slot in engine.get_available_slots(day, day):
            if slot.start == wizard.start:
                return slot.staff
        return []

    def book(self, request, key) -> Booking | None:
        """Book the first candidate still free, or return None when nobody is."""
        wizard = self.wizard
        customer = wizard.customer
        user = request.user if request.user.is_authenticated else None
        for staff in self.candidates():
            try:
                return create_booking(
                    organization=self.organization,
                    service=wizard.service,
                    staff_profile=staff,
                    location=wizard.location,
                    source=Booking.Source.PUBLIC_BOOKING,
                    public=True,
                    start_datetime=wizard.start,
                    customer_name=customer["name"],
                    customer_email=customer["email"],
                    customer_phone=customer.get("phone", ""),
                    customer_notes=customer.get("notes", ""),
                    customer_timezone=wizard.location.timezone,
                    actor=user,
                    customer_user=user,
                    idempotency_key=key,
                )
            except ConflictError as error:
                if error.code == "idempotency_key_reused":
                    raise
                continue
        return None


class ConfirmationView(View):
    """Shown after booking, at an unguessable address (the booking's public UUID). It shows
    what was booked, not who booked it."""

    template_name = "booking/confirmation.html"

    def get(self, request, slug, public_uuid):
        organization = public_organization_or_404(slug)
        booking = get_object_or_404(
            Booking.objects.select_related("service", "staff__user", "location"),
            organization=organization,
            public_uuid=public_uuid,
        )
        zone = booking.location.timezone if booking.location else organization.timezone
        return render(
            request,
            self.template_name,
            {"organization": organization, "booking": booking, "zone": zone},
        )


def theme_stylesheet(request, slug):
    """The organization's brand colours for its booking pages (a stylesheet, because the
    Content Security Policy doesn't allow inline styles)."""
    organization = public_organization_or_404(slug)
    response = HttpResponse(theme_css(organization.brand_color), content_type="text/css")
    response["Cache-Control"] = "public, max-age=300"
    return response
