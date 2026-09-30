"""M5.4: the customer portal - one business at a time (the same email at two businesses never
mixes), nothing internal shown, and cancel / reschedule within the cancellation policy."""

from datetime import time, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bookings.models import Booking, Customer
from bookings.services import create_booking
from core.audit import AuditAction
from core.models import AuditLog
from tests import factories as f

EMAIL = "sam@example.test"


class Fixtures:
    def make_fixtures(self):
        self.user = f.UserFactory(email=EMAIL, email_verified=True, first_name="Sam")
        self.spa = self.business("Glow Spa", "glow")
        self.clinic = self.business("Bright Clinic", "bright")

    def business(self, name, slug):
        organization = f.OrganizationFactory(name=name, slug=slug, timezone="UTC")
        organization.service = f.ServiceFactory(
            organization=organization, name=f"{name} massage", duration_minutes=60
        )
        organization.provider = f.StaffProfileFactory(
            organization=organization, display_name=f"{name} provider"
        )
        f.make_bookable(organization.provider, organization.service, start=time(8), end=time(18))
        return organization

    def book(self, organization, *, days=3, hour=10, user=None, email=EMAIL, name="Sam Lee"):
        return create_booking(
            organization=organization,
            service=organization.service,
            staff_profile=organization.provider,
            customer_name=name,
            customer_email=email,
            start_datetime=f.future(days, hour=hour),
            customer_user=user if user is not None else self.user,
            notify=False,
        )

    def url(self, name, organization, *args):
        return reverse(name, args=[organization.slug, *args])


class IsolationTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.at_spa = self.book(self.spa)
        self.at_clinic = self.book(self.clinic, hour=11)
        self.client.force_login(self.user)

    def test_the_overview_lists_each_business_separately(self):
        response = self.client.get(reverse("portal-home"))
        self.assertContains(response, "Glow Spa")
        self.assertContains(response, "Bright Clinic")
        self.assertContains(response, self.at_spa.service.name)

    def test_one_business_never_shows_the_others_appointments(self):
        for organization, mine, other in (
            (self.spa, self.at_spa, self.at_clinic),
            (self.clinic, self.at_clinic, self.at_spa),
        ):
            for name in ("portal-organization", "portal-appointments"):
                with self.subTest(business=organization.slug, page=name):
                    response = self.client.get(self.url(name, organization))
                    self.assertContains(response, mine.service.name)
                    self.assertNotContains(response, other.service.name)
            # The other business's appointment is not found through this business.
            response = self.client.get(self.url("portal-appointment", organization, other.pk))
            self.assertEqual(response.status_code, 404)

    def test_details_are_per_business(self):
        response = self.client.post(
            self.url("portal-profile", self.spa),
            {"first_name": "Samuel", "last_name": "Lee", "phone": "+15550001111"},
        )
        self.assertRedirects(response, self.url("portal-profile", self.spa))
        spa = Customer.objects.get(organization=self.spa, email=EMAIL)
        clinic = Customer.objects.get(organization=self.clinic, email=EMAIL)
        self.assertEqual((spa.first_name, spa.phone), ("Samuel", "+15550001111"))
        self.assertEqual(clinic.first_name, "Sam")
        response = self.client.get(self.url("portal-profile", self.clinic))
        self.assertNotContains(response, "+15550001111")

    def test_an_unverified_account_sees_nothing_booked_with_its_email(self):
        # Booked as a guest with this address; the account can't prove it owns the address.
        unverified = f.UserFactory(email="kim@example.test", email_verified=False)
        self.book(self.spa, hour=15, user=f.UserFactory(), email="kim@example.test", name="Kim")
        self.client.force_login(unverified)
        self.assertNotContains(self.client.get(reverse("portal-home")), "Glow Spa")
        self.assertEqual(
            self.client.get(self.url("portal-organization", self.spa)).status_code, 404
        )
        # Once verified, it is theirs, at that business only.
        unverified.email_verified = True
        unverified.save()
        self.assertContains(self.client.get(reverse("portal-home")), "Glow Spa")
        self.assertNotContains(self.client.get(reverse("portal-home")), "Bright Clinic")

    def test_other_customers_and_other_businesses_are_404(self):
        stranger = self.book(
            self.spa, hour=14, user=f.UserFactory(), email="x@example.test", name="Stranger"
        )
        self.assertEqual(
            self.client.get(self.url("portal-appointment", self.spa, stranger.pk)).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(self.url("portal-cancel", self.spa, stranger.pk)).status_code, 404
        )
        elsewhere = f.OrganizationFactory(slug="elsewhere")
        self.assertEqual(
            self.client.get(reverse("portal-organization", args=[elsewhere.slug])).status_code,
            404,
        )
        self.spa.is_suspended = True
        self.spa.save()
        self.assertEqual(
            self.client.get(self.url("portal-organization", self.spa)).status_code, 404
        )

    def test_nothing_internal_is_shown(self):
        Booking.objects.filter(pk=self.at_spa.pk).update(internal_notes="TEAM ONLY NOTE")
        customer = self.at_spa.customer
        Customer.objects.filter(pk=customer.pk).update(alerts="TEAM ALERT")
        f.CustomerNoteFactory(organization=self.spa, customer=customer, content="INTERNAL NOTE")
        for name, args in (
            ("portal-organization", ()),
            ("portal-appointments", ()),
            ("portal-appointment", (self.at_spa.pk,)),
            ("portal-profile", ()),
        ):
            with self.subTest(page=name):
                body = self.client.get(self.url(name, self.spa, *args)).content.decode()
                for secret in ("TEAM ONLY NOTE", "TEAM ALERT", "INTERNAL NOTE"):
                    self.assertNotIn(secret, body)

    def test_signing_in_is_required(self):
        self.client.logout()
        response = self.client.get(self.url("portal-organization", self.spa))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("login")))


class PolicyTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.booking = self.book(self.spa)
        self.client.force_login(self.user)

    def test_cancel_within_policy(self):
        page = self.client.get(self.url("portal-appointment", self.spa, self.booking.pk))
        self.assertContains(page, "Cancel appointment")
        response = self.client.post(
            self.url("portal-cancel", self.spa, self.booking.pk), {"reason": "Away"}
        )
        self.assertRedirects(response, self.url("portal-appointment", self.spa, self.booking.pk))
        self.booking.refresh_from_db()
        self.assertEqual(
            (self.booking.status, self.booking.cancellation_reason, self.booking.cancelled_by),
            (Booking.Status.CANCELLED, "Away", self.user),
        )
        history = self.booking.status_history.order_by("-created_at").first()
        self.assertEqual(history.source, Booking.Source.CUSTOMER_PORTAL)

    def test_reschedule_within_policy(self):
        page = self.client.get(self.url("portal-reschedule", self.spa, self.booking.pk))
        self.assertContains(page, "Choose a new time")
        self.assertIn("14:00", [t["label"].strftime("%H:%M") for t in page.context["times"]])
        new_start = self.booking.start_datetime.replace(hour=14)
        response = self.client.post(
            self.url("portal-reschedule", self.spa, self.booking.pk),
            {"start": new_start.isoformat()},
        )
        moved = Booking.objects.get(rescheduled_from=self.booking)
        self.assertRedirects(response, self.url("portal-appointment", self.spa, moved.pk))
        self.assertEqual(moved.start_datetime, new_start)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)
        # The moved appointment is still theirs, in the portal.
        self.assertContains(self.client.get(self.url("portal-appointments", self.spa)), "14:00")

    def test_a_taken_or_off_grid_time_is_refused(self):
        other = self.book(
            self.spa, hour=14, user=f.UserFactory(), email="x@example.test", name="Other"
        )
        for start in (other.start_datetime, self.booking.start_datetime.replace(minute=7)):
            with self.subTest(start=start):
                response = self.client.post(
                    self.url("portal-reschedule", self.spa, self.booking.pk),
                    {"start": start.isoformat()},
                )
                self.assertEqual(response.status_code, 422)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)

    def test_inside_the_deadline_nothing_can_be_changed_online(self):
        soon = timezone.now() + timedelta(hours=2)
        Booking.objects.filter(pk=self.booking.pk).update(
            start_datetime=soon, end_datetime=soon + timedelta(hours=1)
        )
        page = self.client.get(self.url("portal-appointment", self.spa, self.booking.pk))
        self.assertNotContains(page, "Cancel appointment")
        self.assertContains(page, "less than 24 hours before")
        self.assertRedirects(
            self.client.get(self.url("portal-cancel", self.spa, self.booking.pk)),
            self.url("portal-appointment", self.spa, self.booking.pk),
        )
        response = self.client.post(self.url("portal-cancel", self.spa, self.booking.pk))
        self.assertEqual(response.status_code, 422)
        response = self.client.post(
            self.url("portal-reschedule", self.spa, self.booking.pk),
            {"start": f.future(4, hour=10).isoformat()},
        )
        self.assertEqual(response.status_code, 422)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)

    def test_past_and_cancelled_appointments_offer_nothing(self):
        self.client.post(self.url("portal-cancel", self.spa, self.booking.pk))
        page = self.client.get(self.url("portal-appointment", self.spa, self.booking.pk))
        self.assertNotContains(page, "Reschedule")
        self.assertEqual(
            self.client.get(self.url("portal-reschedule", self.spa, self.booking.pk)).status_code,
            302,
        )


class TimeZoneTests(Fixtures, TestCase):
    def test_times_are_the_businesss_wall_clock_time(self):
        self.make_fixtures()
        toronto = f.OrganizationFactory(name="North Clinic", slug="north", timezone="UTC")
        toronto.locations.update(timezone="America/Toronto")
        toronto.service = f.ServiceFactory(organization=toronto, duration_minutes=60)
        toronto.provider = f.StaffProfileFactory(organization=toronto)
        f.make_bookable(toronto.provider, toronto.service)
        booking = self.book(toronto, hour=17)  # 17:00 UTC
        self.client.force_login(self.user)
        local = booking.start_datetime.astimezone(ZoneInfo("America/Toronto"))
        expected = local.strftime("%H:%M")  # 13:00 or 12:00, with daylight saving
        page = self.client.get(self.url("portal-appointment", toronto, booking.pk))
        self.assertContains(page, f"{expected}–")
        self.assertNotContains(page, "17:00–")  # the datetime attribute stays UTC
        page = self.client.get(self.url("portal-reschedule", toronto, booking.pk))
        labels = [t["label"].strftime("%H:%M") for t in page.context["times"]]
        self.assertIn(labels[0], page.content.decode())
        self.assertTrue(all(t["label"].tzinfo is None for t in page.context["times"]))


class ProfileTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.book(self.spa)
        self.client.force_login(self.user)

    def test_consents_are_stamped_and_audited(self):
        response = self.client.post(
            self.url("portal-profile", self.spa),
            {"first_name": "Sam", "last_name": "Lee", "email_consent": "on"},
        )
        self.assertRedirects(response, self.url("portal-profile", self.spa))
        customer = Customer.objects.get(organization=self.spa, email=EMAIL)
        self.assertTrue(customer.email_consent)
        self.assertFalse(customer.marketing_consent)
        self.assertIsNotNone(customer.consent_updated_at)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditAction.CUSTOMER_UPDATED, object_identifier=str(customer.pk)
            ).exists()
        )

    def test_a_name_is_required_and_the_email_cannot_be_changed(self):
        response = self.client.post(
            self.url("portal-profile", self.spa),
            {"first_name": "", "last_name": "", "email": "new@example.test"},
        )
        self.assertEqual(response.status_code, 422)
        customer = Customer.objects.get(organization=self.spa, email=EMAIL)
        self.assertEqual(customer.email, EMAIL)
