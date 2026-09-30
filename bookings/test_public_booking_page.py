"""The public booking wizard /book/<slug>/ (M4.6).

Steps: location → service → provider → time → details → review → confirmation. Steps with one
possible answer are skipped; every step re-checks the earlier answers on the server.
"""

from datetime import time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking
from bookings.services import create_booking
from tests import factories as f
from tests.wizard_helpers import DETAILS, book_through_wizard, first_free_time

URL = "/book/harmony/"


class WizardFixtures:
    def make_fixtures(self):
        self.organization = f.OrganizationFactory(slug="harmony", timezone="America/Toronto")
        self.service = f.ServiceFactory(
            organization=self.organization, is_public=True, name="Deep tissue massage"
        )
        self.private = f.ServiceFactory(
            organization=self.organization, is_public=False, name="Staff-only service"
        )
        self.staff = f.StaffProfileFactory(
            organization=self.organization,
            user=f.UserFactory(first_name="Maya", last_name="Chen", email="maya@private.test"),
        )
        f.make_bookable(self.staff, self.service, start=time(9), end=time(17))

    def path(self, response):
        return urlparse(response["Location"]).path


class WizardFlowTests(WizardFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_single_location_and_provider_are_skipped(self):
        response = self.client.get(URL)
        self.assertRedirects(response, f"{URL}service/")
        body = self.client.get(f"{URL}service/").content.decode()
        self.assertIn("Deep tissue massage", body)
        self.assertNotIn("Staff-only service", body)
        response = self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        self.assertEqual(self.path(response), f"{URL}time/")

    def test_full_booking(self):
        response = book_through_wizard(self.client, self.organization, self.service)
        booking = Booking.objects.get()
        self.assertEqual(
            self.path(response), f"{URL}confirmation/{booking.public_uuid}/", response.content
        )
        self.assertEqual(booking.source, Booking.Source.PUBLIC_BOOKING)
        self.assertEqual(booking.staff, self.staff)
        self.assertTrue(booking.location.is_default)
        self.assertEqual(booking.customer.last_name, "Lovelace")
        self.assertEqual(booking.customer_timezone, booking.location.timezone)
        self.assertTrue(booking.idempotency_key)

        page = self.client.get(response["Location"])
        self.assertContains(page, booking.reference)
        self.assertContains(page, "Maya Chen")
        self.assertNotContains(page, "ada@example.test")  # what was booked, not who
        self.assertNotContains(page, "maya@private.test")
        # The wizard starts over.
        self.assertRedirects(self.client.get(f"{URL}review/"), f"{URL}service/")

    def test_time_step_lists_free_times_in_the_location_time_zone(self):
        self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        start = first_free_time(self.organization, self.service)
        local = start.astimezone(ZoneInfo("America/Toronto"))
        page = self.client.get(f"{URL}time/", {"date": local.date().isoformat()})
        self.assertContains(page, local.strftime("%I:%M %p").lstrip("0"))
        self.assertContains(page, "America/Toronto")

    def test_cannot_skip_ahead(self):
        for step in ("time", "details", "review"):
            with self.subTest(step=step):
                self.assertRedirects(self.client.get(f"{URL}{step}/"), f"{URL}service/")
        self.assertEqual(self.client.post(f"{URL}review/").status_code, 302)
        self.assertFalse(Booking.objects.exists())

    def test_logged_in_customer_is_prefilled_and_linked(self):
        user = f.UserFactory(first_name="Grace", last_name="Hopper", email="grace@example.test")
        self.client.force_login(user)
        self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        start = first_free_time(self.organization, self.service)
        self.client.post(f"{URL}time/", {"start": start.isoformat()})
        self.assertContains(self.client.get(f"{URL}details/"), "grace@example.test")
        book_through_wizard(
            self.client,
            self.organization,
            self.service,
            details={**DETAILS, "email": "grace@example.test"},
        )
        self.assertEqual(Booking.objects.get().customer.user, user)


class AnyProviderAndLocationTests(WizardFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.second = f.StaffProfileFactory(organization=self.organization, display_name="Sam")
        f.make_bookable(self.second, self.service, start=time(9), end=time(17))

    def test_provider_step_offers_anyone(self):
        self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        page = self.client.get(f"{URL}provider/")
        self.assertContains(page, "Anyone available")
        self.assertContains(page, "Sam")

    def test_anyone_books_a_free_provider(self):
        start = first_free_time(self.organization, self.service)
        create_booking(  # Maya is busy then; Sam isn't
            organization=self.organization,
            service=self.service,
            staff_profile=self.staff,
            customer_name="Earlier",
            customer_email="earlier@example.test",
            start_datetime=start,
            notify=False,
        )
        book_through_wizard(self.client, self.organization, self.service, staff="any", start=start)
        booking = Booking.objects.get(customer_email="ada@example.test")
        self.assertEqual(booking.staff, self.second)

    def test_second_location_is_a_choice(self):
        downtown = f.LocationFactory(organization=self.organization, name="Downtown")
        self.assertContains(self.client.get(URL), "Downtown")
        response = self.client.post(URL, {"location": str(downtown.pk)})
        self.assertEqual(self.path(response), f"{URL}service/")
        # Nobody works downtown yet, so there's nothing to book there.
        self.assertNotContains(self.client.get(f"{URL}service/"), "Deep tissue massage")


class TamperingTests(WizardFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_other_tenants_and_private_choices_are_refused(self):
        other_service = f.ServiceFactory(is_public=True)
        for value in (str(other_service.pk), str(self.private.pk), "not-a-uuid"):
            with self.subTest(value=value):
                response = self.client.post(f"{URL}service/", {"service": value})
                self.assertEqual(response.status_code, 422)
        self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        other_staff = f.StaffProfileFactory()
        f.StaffProfileFactory(organization=self.organization)  # makes the provider step real
        response = self.client.post(f"{URL}provider/", {"staff": str(other_staff.pk)})
        self.assertEqual(response.status_code, 422)

    def test_time_must_be_free(self):
        self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        night = first_free_time(self.organization, self.service).replace(hour=3)
        for value in (night.isoformat(), "tomorrow-ish", ""):
            with self.subTest(value=value):
                response = self.client.post(f"{URL}time/", {"start": value})
                self.assertEqual(response.status_code, 422)

    def test_honeypot_and_bad_details(self):
        self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        start = first_free_time(self.organization, self.service)
        self.client.post(f"{URL}time/", {"start": start.isoformat()})
        for override in ({"website": "http://spam.test"}, {"email": "nope"}, {"name": ""}):
            with self.subTest(override=override):
                response = self.client.post(f"{URL}details/", {**DETAILS, **override})
                self.assertEqual(response.status_code, 422)
        self.assertRedirects(self.client.get(f"{URL}review/"), f"{URL}details/")

    def test_slot_taken_between_steps_is_explained(self):
        start = first_free_time(self.organization, self.service)
        self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        self.client.post(f"{URL}time/", {"start": start.isoformat()})
        self.client.post(f"{URL}details/", DETAILS)
        create_booking(
            organization=self.organization,
            service=self.service,
            staff_profile=self.staff,
            customer_name="Faster",
            customer_email="faster@example.test",
            start_datetime=start,
            notify=False,
        )
        response = self.client.post(f"{URL}review/")
        self.assertEqual(self.path(response), f"{URL}time/")
        self.assertContains(self.client.get(f"{URL}time/"), "just booked by someone else")
        self.assertEqual(Booking.objects.count(), 1)

    def test_stale_choice_is_dropped(self):
        self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        self.service.is_public = False
        self.service.save()
        self.assertRedirects(self.client.get(f"{URL}time/"), f"{URL}service/")


class AccessTests(WizardFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_suspended_or_disabled_organizations_have_no_pages(self):
        for field, value in (("is_suspended", True), ("booking_page_enabled", False)):
            with self.subTest(field=field):
                setattr(self.organization, field, value)
                self.organization.save()
                for path in ("", "service/", "time/", "review/", "theme.css"):
                    self.assertEqual(self.client.get(f"{URL}{path}").status_code, 404)
                setattr(self.organization, field, not value)
                self.organization.save()

    def test_guest_booking_can_be_turned_off(self):
        self.organization.allow_guest_booking = False
        self.organization.save()
        self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        start = first_free_time(self.organization, self.service)
        self.client.post(f"{URL}time/", {"start": start.isoformat()})
        self.assertContains(self.client.get(f"{URL}details/"), "Sign in to continue")
        self.client.post(f"{URL}details/", DETAILS)
        self.assertEqual(self.client.post(f"{URL}review/").status_code, 403)
        self.client.force_login(f.UserFactory())
        response = self.client.post(f"{URL}review/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Booking.objects.exists())

    @override_settings(PUBLIC_BOOKING_RATE="1/3600")
    def test_confirmations_are_rate_limited(self):
        from django.core.cache import cache

        cache.clear()
        book_through_wizard(self.client, self.organization, self.service)
        response = book_through_wizard(
            self.client, self.organization, self.service, details={**DETAILS, "email": "b@x.test"}
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(Booking.objects.count(), 1)
        cache.clear()

    def test_confirmation_needs_the_right_organization(self):
        book_through_wizard(self.client, self.organization, self.service)
        booking = Booking.objects.get()
        other = f.OrganizationFactory(slug="elsewhere")
        self.assertEqual(
            self.client.get(f"/book/{other.slug}/confirmation/{booking.public_uuid}/").status_code,
            404,
        )

    def test_brand_stylesheet(self):
        self.assertEqual(self.client.get(f"{URL}theme.css").content, b"")
        self.organization.brand_color = "teal"
        self.organization.booking_instructions = "Free parking behind the building."
        self.organization.save()
        css = self.client.get(f"{URL}theme.css")
        self.assertEqual(css["Content-Type"], "text/css")
        self.assertIn(b"--color-brand-600:#0f766e", css.content)
        page = self.client.get(f"{URL}service/")
        self.assertContains(page, "/book/harmony/theme.css")
        self.assertContains(page, "Free parking behind the building.")

    def test_wizards_for_two_organizations_do_not_mix(self):
        other = f.OrganizationFactory(slug="other")
        other_service = f.ServiceFactory(organization=other, is_public=True)
        f.make_bookable(f.StaffProfileFactory(organization=other), other_service)
        self.client.post(f"{URL}service/", {"service": str(self.service.pk)})
        self.client.post("/book/other/service/", {"service": str(other_service.pk)})
        self.assertContains(self.client.get(f"{URL}time/"), "Deep tissue massage")


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
