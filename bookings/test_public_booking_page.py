"""The server-rendered public booking page /book/<slug>/ (codebase review, 2026-09-28)."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking
from tests import factories as f

TORONTO = ZoneInfo("America/Toronto")


class PublicBookingPageTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory(slug="harmony", timezone="America/Toronto")
        self.service = f.ServiceFactory(organization=self.organization, is_public=True)
        self.private = f.ServiceFactory(
            organization=self.organization, is_public=False, name="Staff-only service"
        )
        self.staff = f.StaffProfileFactory(
            organization=self.organization,
            user=f.UserFactory(first_name="Maya", last_name="Chen", email="maya@private.test"),
        )
        f.StaffServiceOfferingFactory(
            organization=self.organization, staff=self.staff, service=self.service
        )
        self.url = "/book/harmony/"
        self.start = datetime.combine(
            timezone.localdate() + timedelta(days=7), datetime.min.time().replace(hour=10)
        )
        f.WeeklyAvailabilityFactory(
            organization=self.organization,
            staff=self.staff,
            day_of_week=self.start.weekday(),
            start_time=time(9),
            end_time=time(17),
        )

    def payload(self, **overrides):
        return {
            "service": str(self.service.pk),
            "staff": str(self.staff.pk),
            "start_datetime": self.start.strftime("%Y-%m-%dT%H:%M"),  # datetime-local: no offset
            "customer_name": "Ada Lovelace",
            "customer_email": "ada@example.test",
            **overrides,
        }

    def test_page_lists_public_services_and_hides_staff_emails(self):
        body = self.client.get(self.url).content.decode()
        self.assertIn(self.service.name, body)
        self.assertNotIn("Staff-only service", body)
        self.assertIn("Maya Chen", body)
        self.assertNotIn("maya@private.test", body)

    def test_suspended_or_disabled_organizations_have_no_page(self):
        for field, value in (("is_suspended", True), ("booking_page_enabled", False)):
            with self.subTest(field=field):
                setattr(self.organization, field, value)
                self.organization.save()
                self.assertEqual(self.client.get(self.url).status_code, 404)
                self.assertEqual(self.client.post(self.url, self.payload()).status_code, 404)
                setattr(self.organization, field, not value)
                self.organization.save()
        self.assertFalse(Booking.objects.exists())

    def test_valid_booking_uses_the_organization_time_zone(self):
        response = self.client.post(self.url, self.payload())
        booking = Booking.objects.get()
        self.assertRedirects(
            response, f"/book/success/{booking.reference}/", fetch_redirect_response=False
        )
        self.assertEqual(booking.start_datetime, self.start.replace(tzinfo=TORONTO))
        self.assertEqual(booking.customer.last_name, "Lovelace")
        self.assertEqual(booking.customer_timezone, "America/Toronto")

    def test_bad_input_is_a_form_error_not_a_crash(self):
        cases = {
            "private service": {"service": str(self.private.pk)},
            "malformed id": {"service": "not-a-uuid"},
            "other tenant's staff": {"staff": str(f.StaffProfileFactory().pk)},
            "bad time": {"start_datetime": "tomorrow-ish"},
            "bad email": {"customer_email": "nope"},
            "missing name": {"customer_name": ""},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                response = self.client.post(self.url, self.payload(**override))
                self.assertEqual(response.status_code, 400)
        self.assertFalse(Booking.objects.exists())

    def test_past_time_and_taken_slot_are_explained(self):
        past = (self.start - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(self.url, self.payload(start_datetime=past))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "past", status_code=400)

        self.client.post(self.url, self.payload())
        response = self.client.post(self.url, self.payload(customer_email="b@example.test"))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "no longer available", status_code=409)
        self.assertEqual(Booking.objects.count(), 1)

    def test_guest_booking_can_be_turned_off(self):
        self.organization.allow_guest_booking = False
        self.organization.save()
        self.assertContains(self.client.get(self.url), "sign in")
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 403)
        self.client.force_login(f.UserFactory())
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 302)


class PublicApiBookingTests(TestCase):
    def test_self_service_api_cannot_book_a_private_service(self):
        organization = f.OrganizationFactory(slug="harmony")
        private = f.ServiceFactory(organization=organization, is_public=False)
        staff = f.StaffProfileFactory(organization=organization)
        client = APIClient()
        client.force_authenticate(f.UserFactory())
        payload = {
            "service": str(private.pk),
            "staff": str(staff.pk),
            "start_datetime": (timezone.now() + timedelta(days=3)).isoformat(),
            "customer_name": "Ada",
            "customer_email": "ada@example.test",
        }
        response = client.post("/api/v1/bookings/", payload, HTTP_X_ORGANIZATION_SLUG="harmony")
        self.assertEqual(response.status_code, 400)
        response = client.post(
            "/api/v1/bookings/",
            {**payload, "customer_timezone": "Mars/Olympus"},
            HTTP_X_ORGANIZATION_SLUG="harmony",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Booking.objects.exists())
