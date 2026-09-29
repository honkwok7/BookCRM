from zoneinfo import ZoneInfo

from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from bookings.forms import PublicBookingForm
from bookings.services import create_booking
from core.exceptions import ConflictError, DomainError
from organizations.tenancy import get_public_organization


class PublicBookingPageView(View):
    """The organization's public booking page (replaced by the booking wizard in M4.6)."""

    template_name = "web/public_booking.html"

    def _organization(self, slug):
        # Same rule as the public API: active, not suspended, booking page enabled.
        organization = get_public_organization(slug)
        if organization is None:
            raise Http404
        return organization

    def _render(self, request, organization, form, status=200):
        context = {"organization": organization, "form": form}
        if not organization.allow_guest_booking and not request.user.is_authenticated:
            context["sign_in_required"] = True
        return render(request, self.template_name, context, status=status)

    def get(self, request, slug):
        organization = self._organization(slug)
        return self._render(request, organization, PublicBookingForm(organization=organization))

    def post(self, request, slug):
        organization = self._organization(slug)
        form = PublicBookingForm(request.POST, organization=organization)
        if not organization.allow_guest_booking and not request.user.is_authenticated:
            return self._render(request, organization, form, status=403)
        with timezone.override(ZoneInfo(organization.timezone)):
            valid = form.is_valid()
        if not valid:
            return self._render(request, organization, form, status=409 if form.conflict else 400)

        data = form.cleaned_data
        user = request.user if request.user.is_authenticated else None
        try:
            booking = create_booking(
                organization=organization,
                service=data["service"],
                staff_profile=data["staff"],
                customer_name=data["customer_name"],
                customer_email=data["customer_email"],
                customer_phone=data["customer_phone"],
                start_datetime=data["start_datetime"],
                customer_timezone=organization.timezone,
                customer_notes=data["customer_notes"],
                actor=user,
                customer_user=user,
            )
        except DomainError as error:
            form.add_error(None, error.message)
            status = 409 if isinstance(error, ConflictError) else 400
            return self._render(request, organization, form, status=status)
        return redirect("booking-success", reference=booking.reference)


class BookingSuccessView(View):
    template_name = "web/booking_success.html"

    def get(self, request, reference):
        return render(request, self.template_name, {"reference": reference})
