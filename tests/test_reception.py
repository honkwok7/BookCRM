"""M5.2: the reception dashboard - access, today's timeline, waiting, providers now,
cancellations, the waitlist count, row actions, the walk-in path, the next free time and the
query budget of the live refresh."""

from datetime import time
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from bookings.models import Booking
from bookings.reception import front_desk
from bookings.services import cancel_booking, change_booking_status, create_booking
from bookings.waitlist import join_waitlist
from locations.models import Location
from organizations.models import OrganizationRole
from organizations.permissions import ROLE_CAPABILITIES, Capability
from tests import factories as f

Status = Booking.Status
URL = "/app/reception/"


class Fixtures:
    def make_fixtures(self):
        cache.clear()
        self.org = f.OrganizationFactory(slug="glow", timezone="UTC")
        self.location = Location.objects.get(organization=self.org, is_default=True)
        self.service = f.ServiceFactory(organization=self.org, name="Massage", duration_minutes=60)
        self.maya = f.StaffProfileFactory(organization=self.org, display_name="Maya")
        self.sam = f.StaffProfileFactory(organization=self.org, display_name="Sam")
        f.make_bookable(self.maya, self.service, start=time(8), end=time(18))
        f.make_bookable(self.sam, self.service, start=time(8), end=time(12))
        # Noon tomorrow (UTC): Maya works, Sam's hours ended.
        self.noon = f.future(1, hour=12)
        self.receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user

    def book(self, hour, minute=0, staff=None, email="a@x.test"):
        return create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=staff or self.maya,
            customer_name=f"Customer {hour}:{minute:02d}",
            customer_email=email,
            start_datetime=self.noon.replace(hour=hour, minute=minute),
            notify=False,
        )

    def at_noon(self):
        return patch("django.utils.timezone.now", return_value=self.noon)


class AccessTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_the_front_desk_roles_can_open_it(self):
        for role in (
            OrganizationRole.RECEPTIONIST,
            OrganizationRole.MANAGER,
            OrganizationRole.OWNER,
        ):
            with self.subTest(role=role):
                self.client.force_login(f.MembershipFactory(organization=self.org, role=role).user)
                self.assertEqual(self.client.get(URL).status_code, 200)

    def test_providers_and_customers_cannot(self):
        for role in (OrganizationRole.STAFF, OrganizationRole.CUSTOMER):
            with self.subTest(role=role):
                self.client.force_login(f.MembershipFactory(organization=self.org, role=role).user)
                self.assertEqual(self.client.get(URL).status_code, 403)

    def test_receptionists_get_no_settings_or_billing(self):
        # The settings and subscription pages arrive later; their capabilities stay out of the
        # receptionist role, and nothing on the front desk links to them.
        capabilities = ROLE_CAPABILITIES[OrganizationRole.RECEPTIONIST]
        for capability in (Capability.ORGANIZATION_MANAGE, Capability.BILLING_MANAGE):
            self.assertNotIn(capability, capabilities)
        self.client.force_login(self.receptionist)
        body = self.client.get(URL).content.decode().lower()
        for word in ("/settings", "/subscription", "billing"):
            self.assertNotIn(word, body)

    def test_receptionists_land_here_after_signing_in(self):
        self.client.force_login(self.receptionist)
        self.assertRedirects(self.client.get(reverse("home")), URL)


class FrontDeskTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.early = self.book(9, staff=self.sam)
        self.now_on = self.book(11, 30)  # Maya, 11:30-12:30: on at noon
        self.later = self.book(15)
        self.gone = self.book(16)
        with self.at_noon():  # cancelled today (the desk's "today" is tomorrow)
            cancel_booking(booking=self.gone, reason="ill")
        other = f.OrganizationFactory()
        f.BookingFactory(organization=other, start_datetime=self.noon, customer_name="Stranger")
        join_waitlist(
            organization=self.org,
            service=self.service,
            customer_name="W",
            customer_email="w@x.test",
        )
        with patch("django.utils.timezone.now", return_value=self.noon.replace(hour=11, minute=25)):
            change_booking_status(booking=self.now_on, new_status=Status.CHECKED_IN)

    def desk(self):
        return front_desk(self.org, self.location, now=self.noon)

    def test_today_waiting_cancellations_and_waitlist(self):
        desk = self.desk()
        self.assertEqual(
            [b.pk for b in desk["timeline"]],
            [self.early.pk, self.now_on.pk, self.later.pk, self.gone.pk],
        )
        self.assertEqual([b.pk for b in desk["waiting"]], [self.now_on.pk])
        self.assertEqual([b.pk for b in desk["cancellations"]], [self.gone.pk])
        self.assertEqual(desk["waitlist_count"], 1)

    def test_what_each_provider_is_doing_now(self):
        board = {row.staff.pk: row for row in self.desk()["providers"]}
        self.assertEqual(board[self.maya.pk].state, "busy")
        self.assertEqual(board[self.maya.pk].current, self.now_on)
        self.assertEqual(board[self.maya.pk].next, self.later)
        self.assertEqual(board[self.sam.pk].state, "off")  # his hours ended at 12:00
        with patch("django.utils.timezone.now", return_value=self.noon):
            change_booking_status(booking=self.now_on, new_status=Status.COMPLETED)
        board = {row.staff.pk: row for row in self.desk()["providers"]}
        self.assertEqual(board[self.maya.pk].state, "free")

    def test_the_page_shows_only_this_organization(self):
        self.client.force_login(self.receptionist)
        with self.at_noon():
            response = self.client.get(URL)
        self.assertContains(response, "Customer 11:30")
        self.assertNotContains(response, "Stranger")
        self.assertContains(response, "Waitlist: 1 waiting")

    def test_the_live_refresh_is_a_fragment_within_budget(self):
        self.client.force_login(self.receptionist)
        with self.at_noon(), CaptureQueriesContext(connection) as queries:
            response = self.client.get(URL, HTTP_HX_REQUEST="true")
        body = response.content.decode()
        self.assertIn('id="reception-live"', body)
        self.assertNotIn("<html", body)
        self.assertLessEqual(len(queries), 14, [q["sql"][:80] for q in queries])


class ActionTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.booking = self.book(12)
        self.client.force_login(self.receptionist)

    def post(self, action, next_url):
        with self.at_noon():
            return self.client.post(
                reverse("app-appointment-action", args=[self.booking.pk]),
                {"action": action, "next": next_url},
            )

    def test_check_in_from_the_desk_comes_back_to_the_desk(self):
        response = self.post("check_in", f"{URL}?location={self.location.pk}")
        self.assertRedirects(
            response, f"{URL}?location={self.location.pk}", fetch_redirect_response=False
        )
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Status.CHECKED_IN)

    def test_next_outside_the_app_is_ignored(self):
        for target in ("https://evil.example/", "//evil.example/app/", "/admin/"):
            with self.subTest(target=target):
                response = self.post("confirm", target)
                self.assertRedirects(
                    response,
                    reverse("app-appointment", args=[self.booking.pk]),
                    fetch_redirect_response=False,
                )

    def test_rows_offer_the_actions_the_workflow_allows(self):
        with self.at_noon():
            response = self.client.get(URL)
        row = next(b for b in response.context["desk"]["timeline"] if b.pk == self.booking.pk)
        self.assertEqual([action for action, _ in row.row_actions], ["check_in", "no_show"])
        self.assertTrue(row.can_reschedule)


class WalkInTests(Fixtures, TestCase):
    """Acceptance: a walk-in is checked in within three interactions from the front desk."""

    def test_open_the_desk_click_walk_in_submit(self):
        self.make_fixtures()
        customer = f.CustomerFactory(organization=self.org)
        self.client.force_login(self.receptionist)
        with self.at_noon():
            desk = self.client.get(URL)  # 1: the front desk
            self.assertContains(desk, reverse("app-appointment-walk-in"))
            form = self.client.get(reverse("app-appointment-walk-in"))  # 2: Walk-in
            self.assertEqual(form.status_code, 200)
            done = self.client.post(  # 3: submit
                reverse("app-appointment-walk-in"),
                {"service": str(self.service.pk), "staff": "any", "customer": str(customer.pk)},
            )
        self.assertEqual(done.status_code, 302)
        self.assertEqual(Booking.objects.get().status, Status.CHECKED_IN)


class NextFreeTests(Fixtures, TestCase):
    def test_next_free_time_per_service_is_cached(self):
        self.make_fixtures()
        self.book(12)
        self.client.force_login(self.receptionist)
        url = reverse("app-reception-next-free")
        with self.at_noon():
            response = self.client.get(url, HTTP_HX_REQUEST="true")
            self.assertContains(response, "Massage")
            row = response.context["rows"][0]
            self.assertEqual(row["start"], self.noon.replace(hour=13))  # after the 12:00 one
            with CaptureQueriesContext(connection) as cached:
                self.client.get(url, HTTP_HX_REQUEST="true")
        with CaptureQueriesContext(connection) as fresh:
            cache.clear()
            with self.at_noon():
                self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertLess(len(cached), len(fresh))

    def test_without_javascript_it_is_a_page(self):
        self.make_fixtures()
        self.client.force_login(self.receptionist)
        with self.at_noon():
            response = self.client.get(reverse("app-reception-next-free"))
        self.assertContains(response, "<html")
        self.assertContains(response, "Back to reception")
