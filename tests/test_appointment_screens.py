"""M4.5: reception and provider appointment screens (list, new, walk-in, reschedule)."""

from datetime import time, timedelta
from unittest.mock import patch

from django.test import TestCase

from bookings.models import Booking, Customer
from bookings.services import change_booking_status, create_booking
from locations.models import Location
from organizations.models import OrganizationRole
from scheduling.availability import AvailabilityService
from tests import factories as f

Status = Booking.Status
HTMX = {"HTTP_HX_REQUEST": "true"}


class Fixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(timezone="UTC")
        self.receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user
        self.provider_user = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.STAFF
        ).user
        self.service = f.ServiceFactory(organization=self.org, name="Massage", duration_minutes=60)
        self.maya = f.StaffProfileFactory(
            organization=self.org, user=self.provider_user, display_name="Maya"
        )
        self.sam = f.StaffProfileFactory(organization=self.org, display_name="Sam")
        for staff in (self.maya, self.sam):
            f.make_bookable(staff, self.service, start=time(9), end=time(17))
        self.customer = f.CustomerFactory(
            organization=self.org, first_name="Ada", last_name="Lovelace", email="ada@x.test"
        )
        self.day = f.future(2).date()

    def slot(self, staff=None, day=None):
        engine = AvailabilityService(self.org, self.service)
        return engine.get_available_slots(day or self.day, day or self.day, staff=staff)[0].start

    def payload(self, **overrides):
        return {
            "location": str(Location.objects.get(organization=self.org, is_default=True).pk),
            "service": str(self.service.pk),
            "staff": str(self.maya.pk),
            "date": self.day.isoformat(),
            "start": self.slot(self.maya).isoformat(),
            "customer": str(self.customer.pk),
            **overrides,
        }


class ListTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        start = self.slot(self.maya)
        for staff, name in ((self.maya, "Ada Lovelace"), (self.sam, "Grace Hopper")):
            create_booking(
                organization=self.org,
                service=self.service,
                staff_profile=staff,
                customer_name=name,
                customer_email=f"{name.split()[0].lower()}@x.test",
                start_datetime=start,
                notify=False,
            )

    def test_reception_sees_everyone_upcoming(self):
        self.client.force_login(self.receptionist)
        response = self.client.get("/app/appointments/")
        self.assertContains(response, "Ada Lovelace")
        self.assertContains(response, "Grace Hopper")
        self.assertContains(response, "New appointment")

    def test_search_and_filters(self):
        self.client.force_login(self.receptionist)
        response = self.client.get("/app/appointments/", {"q": "grace"}, **HTMX)
        self.assertNotContains(response, "Ada Lovelace")
        self.assertNotContains(response, "<html")
        response = self.client.get("/app/appointments/", {"when": "past"})
        self.assertNotContains(response, "Grace Hopper")

    def test_providers_see_their_own(self):
        self.client.force_login(self.provider_user)
        response = self.client.get("/app/appointments/")
        self.assertContains(response, "Ada Lovelace")
        self.assertNotContains(response, "Grace Hopper")

    def test_customers_are_refused(self):
        customer = f.MembershipFactory(organization=self.org, role=OrganizationRole.CUSTOMER).user
        self.client.force_login(customer)
        self.assertEqual(self.client.get("/app/appointments/").status_code, 403)


class NewAppointmentTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.receptionist)

    def post(self, data, **headers):
        return self.client.post("/app/appointments/new/", data, **{**HTMX, **headers})

    def test_form_refreshes_its_choices(self):
        response = self.post(
            {
                "q": "lovelace",
                "service": str(self.service.pk),
                "date": self.day.isoformat(),
                "refresh": "1",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ada Lovelace")
        self.assertContains(response, "First available")
        body = response.content.decode()
        self.assertTrue("(Maya, Sam)" in body or "(Sam, Maya)" in body)  # who is free

    def test_book_for_an_existing_customer(self):
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 204, response.content)
        booking = Booking.objects.get()
        self.assertEqual(response["HX-Redirect"], f"/app/appointments/{booking.pk}/")
        self.assertEqual(booking.customer, self.customer)
        self.assertEqual(booking.source, Booking.Source.RECEPTION)
        self.assertEqual(booking.staff, self.maya)

    def test_book_for_a_new_phone_only_customer(self):
        data = self.payload(customer="", name="Walk Smith", phone="+1 416 555 0199")
        self.assertEqual(self.post(data).status_code, 204)
        customer = Customer.objects.get(name="Walk Smith")
        self.assertEqual((customer.email, customer.phone), ("", "+1 416 555 0199"))

    def test_customer_details_are_required(self):
        response = self.post(self.payload(customer="", name="Nobody"))
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "email address or a phone number", status_code=422)

    def test_first_available(self):
        start = self.slot(self.maya)
        create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.maya,
            customer_name="Busy",
            customer_email="busy@x.test",
            start_datetime=start,
            notify=False,
        )
        response = self.post(self.payload(staff="any", start=start.isoformat()))
        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(Booking.objects.get(customer=self.customer).staff, self.sam)

    def test_a_time_that_is_not_free_is_refused(self):
        night = self.slot(self.maya).replace(hour=22)
        response = self.post(self.payload(start=night.isoformat()))
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Booking.objects.exists())

    def test_providers_book_only_for_themselves(self):
        self.client.force_login(self.provider_user)
        # A provider can't look up customers they have never seen: they enter the details.
        new_customer = {"customer": "", "name": "Walk Smith", "email": "walk@x.test"}
        response = self.post(self.payload(staff=str(self.sam.pk), **new_customer))
        self.assertEqual(response.status_code, 422)
        response = self.post(self.payload(**new_customer))
        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(Booking.objects.get().source, Booking.Source.STAFF)

    def test_booking_needs_appointments_manage(self):
        viewer = f.MembershipFactory(
            organization=self.org,
            role=OrganizationRole.RECEPTIONIST,
            revoked_permissions=["appointments.manage"],
        ).user
        self.client.force_login(viewer)
        self.assertEqual(self.client.get("/app/appointments/new/").status_code, 403)
        self.assertEqual(self.post(self.payload()).status_code, 403)

    def test_page_without_javascript(self):
        response = self.client.post("/app/appointments/new/", self.payload())
        booking = Booking.objects.get()
        self.assertRedirects(
            response, f"/app/appointments/{booking.pk}/", fetch_redirect_response=False
        )


class WalkInTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.receptionist)

    def test_walk_in_starts_now_and_is_checked_in(self):
        noon = f.future(1, hour=12)
        with patch("django.utils.timezone.now", return_value=noon):
            response = self.client.post(
                "/app/appointments/walk-in/",
                {
                    "service": str(self.service.pk),
                    "staff": "any",
                    "customer": str(self.customer.pk),
                },
                **HTMX,
            )
        self.assertEqual(response.status_code, 204, response.content)
        booking = Booking.objects.get()
        self.assertEqual(booking.status, Status.CHECKED_IN)
        self.assertEqual(booking.start_datetime, noon + timedelta(minutes=1))

    def test_walk_in_outside_hours_is_explained(self):
        night = f.future(1, hour=22)
        with patch("django.utils.timezone.now", return_value=night):
            response = self.client.post(
                "/app/appointments/walk-in/",
                {
                    "service": str(self.service.pk),
                    "staff": "any",
                    "customer": str(self.customer.pk),
                },
                **HTMX,
            )
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "Nobody is free", status_code=422)


class RescheduleTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.receptionist)
        self.booking = create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.maya,
            customer_name="Ada Lovelace",
            customer_email="ada@x.test",
            start_datetime=self.slot(self.maya),
            notify=False,
        )
        self.url = f"/app/appointments/{self.booking.pk}/reschedule/"

    def test_form_offers_the_providers_free_times_including_overlapping_its_own(self):
        response = self.client.get(self.url, **HTMX)
        self.assertContains(response, "9:15 AM")  # overlaps the 9:00 appointment being moved

    def test_reschedule(self):
        new_start = self.booking.start_datetime + timedelta(hours=3)
        response = self.client.post(
            self.url, {"date": self.day.isoformat(), "start": new_start.isoformat()}, **HTMX
        )
        self.assertEqual(response.status_code, 204, response.content)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Status.CANCELLED)
        moved = Booking.objects.get(rescheduled_from=self.booking)
        self.assertEqual(moved.start_datetime, new_start)
        self.assertEqual(moved.status_history.get().source, Booking.Source.RECEPTION)

    def test_finished_appointments_cannot_be_rescheduled(self):
        f.make_current(self.booking)
        change_booking_status(booking=self.booking, new_status=Status.NO_SHOW)
        response = self.client.post(self.url, {"date": self.day.isoformat(), "start": "x"}, **HTMX)
        self.assertEqual(response.status_code, 422)

    def test_panel_offers_reschedule(self):
        response = self.client.get(f"/app/appointments/{self.booking.pk}/")
        self.assertContains(response, "Reschedule")
