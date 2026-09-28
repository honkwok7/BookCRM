from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime
from django.views import View

from bookings.services import create_booking
from organizations.models import Organization
from services.models import Service
from staff.models import StaffProfile


class PublicBookingPageView(View):
    template_name = "web/public_booking.html"

    def get(self, request, slug):
        organization = get_object_or_404(
            Organization, slug=slug, booking_page_enabled=True, is_active=True
        )
        services = Service.objects.filter(
            organization=organization, is_public=True, is_active=True, is_archived=False
        )
        staff = StaffProfile.objects.filter(
            organization=organization, is_active=True, is_accepting_bookings=True
        )
        return render(
            request,
            self.template_name,
            {"organization": organization, "services": services, "staff": staff},
        )

    def post(self, request, slug):
        organization = get_object_or_404(
            Organization, slug=slug, booking_page_enabled=True, is_active=True
        )
        service = get_object_or_404(
            Service, id=request.POST.get("service"), organization=organization
        )
        staff_profile = get_object_or_404(
            StaffProfile, id=request.POST.get("staff"), organization=organization
        )
        start_datetime = parse_datetime(request.POST.get("start_datetime", ""))
        if not start_datetime:
            return render(
                request,
                self.template_name,
                {
                    "organization": organization,
                    "services": Service.objects.filter(
                        organization=organization, is_public=True, is_active=True, is_archived=False
                    ),
                    "staff": StaffProfile.objects.filter(
                        organization=organization, is_active=True, is_accepting_bookings=True
                    ),
                    "error": "Invalid start datetime format",
                },
                status=400,
            )

        booking = create_booking(
            organization=organization,
            service=service,
            staff_profile=staff_profile,
            customer_name=request.POST.get("customer_name"),
            customer_email=request.POST.get("customer_email"),
            customer_phone=request.POST.get("customer_phone", ""),
            start_datetime=start_datetime,
            customer_timezone=request.POST.get("customer_timezone", "UTC"),
            customer_notes=request.POST.get("customer_notes", ""),
        )
        return redirect("booking-success", reference=booking.reference)


class BookingSuccessView(View):
    template_name = "web/booking_success.html"

    def get(self, request, reference):
        return render(request, self.template_name, {"reference": reference})
