"""M4.7: the waitlist - joining, matching a freed time, notifying once, and the screens."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from importlib import import_module
from unittest import skipUnless
from unittest.mock import patch

from django.apps import apps
from django.core import mail
from django.db import IntegrityError, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from bookings import waitlist
from bookings.models import Booking, WaitlistEntry
from bookings.services import cancel_booking, create_booking, reschedule_booking
from bookings.tasks import notify_waitlist
from bookings.waitlist import (
    NOTIFY_LIMIT,
    join_waitlist,
    matching_entries,
    notify_matching_entries,
)
from core.exceptions import DomainError
from crm.models import CustomerActivity
from notifications.models import NotificationLog
from notifications.tasks import send_templated_email
from organizations.models import OrganizationRole
from tests import factories as f
from tests.wizard_helpers import DETAILS

TimeOfDay = WaitlistEntry.TimeOfDay
POSTGRES = skipUnless(connection.vendor == "postgresql", "row locks across connections")


class Fixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(slug="glow", timezone="UTC")
        self.service = f.ServiceFactory(organization=self.org, name="Massage", is_public=True)
        self.staff = f.StaffProfileFactory(organization=self.org, display_name="Maya")
        f.make_bookable(self.staff, self.service, start=time(8), end=time(20))
        self.day = f.future(3).date()

    def book(self, hour=10, email="booked@x.test", **kwargs):
        return create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.staff,
            customer_name="Booked Person",
            customer_email=email,
            start_datetime=f.future(3, hour=hour),
            notify=False,
            **kwargs,
        )

    def join(self, email="wait@x.test", **kwargs):
        options = {
            "organization": self.org,
            "service": self.service,
            "customer_name": "Wendy Waiter",
            "customer_email": email,
        }
        options.update(kwargs)
        return join_waitlist(**options)


class JoinTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_join_links_the_customer_and_joining_again_updates(self):
        entry = self.join(time_of_day=TimeOfDay.MORNING)
        self.assertEqual(entry.customer.email, "wait@x.test")
        self.assertIsNotNone(entry.expires_at)
        again = self.join(time_of_day=TimeOfDay.EVENING)
        self.assertEqual(again.pk, entry.pk)
        self.assertEqual(WaitlistEntry.objects.get().time_of_day, TimeOfDay.EVENING)

    def test_validation(self):
        with self.assertRaises(DomainError):
            self.join(service=f.ServiceFactory())  # another organization's service
        with self.assertRaises(DomainError):
            self.join(email="")
        today = timezone.now().date()
        with self.assertRaises(DomainError):
            self.join(preferred_start_date=today + timedelta(days=5), preferred_end_date=today)
        with self.assertRaises(DomainError):
            self.join(preferred_end_date=today - timedelta(days=1))

    def test_impossible_preferences_are_refused(self):
        stranger = f.StaffProfileFactory(organization=self.org)  # doesn't offer the service
        empty = f.LocationFactory(organization=self.org)  # nobody offers it there
        for preferences in (
            {"preferred_staff": stranger},
            {"location": empty},
            {"preferred_staff": self.staff, "location": empty},
        ):
            with self.subTest(preferences=preferences):
                with self.assertRaises(DomainError) as raised:
                    self.join(**preferences)
                self.assertEqual(raised.exception.code, "invalid_preferences")
        self.assertFalse(WaitlistEntry.objects.exists())

    def test_one_waiting_entry_per_customer_and_service_in_the_database(self):
        entry = self.join()
        with self.assertRaises(IntegrityError):
            WaitlistEntry.objects.create(
                organization=self.org,
                service=self.service,
                customer=entry.customer,
                customer_name="Twice",
                customer_email="wait@x.test",
            )

    def test_migration_keeps_the_oldest_waiting_entry(self):
        constraint = next(
            item
            for item in WaitlistEntry._meta.constraints
            if item.name == waitlist.ONE_WAITING_CONSTRAINT
        )
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # no pending FK checks
        with connection.schema_editor() as editor:
            editor.remove_constraint(WaitlistEntry, constraint)
        first = self.join()
        second = f.WaitlistEntryFactory(
            organization=self.org, service=self.service, customer=first.customer
        )
        migration = import_module("bookings.migrations.0014_waitlist_one_waiting_entry")
        migration.close_duplicates(apps, None)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, WaitlistEntry.Status.WAITING)
        self.assertEqual(second.status, WaitlistEntry.Status.CLOSED)
        with connection.schema_editor() as editor:
            editor.add_constraint(WaitlistEntry, constraint)

    def test_end_date_sets_expiry(self):
        end = self.day + timedelta(days=2)
        entry = self.join(preferred_end_date=end)
        self.assertEqual(entry.expires_at.date(), end + timedelta(days=1))


class MatchingTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.freed = self.book(hour=10)  # a morning appointment that will be cancelled

    def matches(self):
        return [entry.customer_email for entry in matching_entries(self.freed)]

    def test_preferences(self):
        self.join("any@x.test")
        self.join("morning@x.test", time_of_day=TimeOfDay.MORNING)
        self.join("evening@x.test", time_of_day=TimeOfDay.EVENING)
        self.join("later@x.test", preferred_start_date=self.day + timedelta(days=1))
        sam = f.make_bookable(f.StaffProfileFactory(organization=self.org), self.service)
        self.join("sam@x.test", preferred_staff=sam)
        elsewhere = f.LocationFactory(organization=self.org)
        sam.locations.add(elsewhere)
        self.join("elsewhere@x.test", location=elsewhere)
        other = f.ServiceFactory(organization=self.org)
        self.join("other@x.test", service=other)
        self.assertEqual(self.matches(), ["any@x.test", "morning@x.test"])

    def test_expired_closed_and_notified_entries_do_not_match(self):
        expired = self.join("expired@x.test")
        WaitlistEntry.objects.filter(pk=expired.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        closed = self.join("closed@x.test")
        WaitlistEntry.objects.filter(pk=closed.pk).update(status=WaitlistEntry.Status.CLOSED)
        self.assertEqual(self.matches(), [])

    def test_a_time_booked_again_is_not_offered(self):
        self.join()
        cancel_booking(booking=self.freed)
        self.book(hour=10, email="quick@x.test")
        self.assertEqual(self.matches(), [])

    def test_past_times_are_not_offered(self):
        self.join()
        Booking.objects.filter(pk=self.freed.pk).update(
            start_datetime=timezone.now() - timedelta(hours=2),
            end_datetime=timezone.now() - timedelta(hours=1),
        )
        self.freed.refresh_from_db()
        self.assertEqual(self.matches(), [])


@patch("notifications.services.send_templated_email.delay")
class NotificationTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.freed = self.book(hour=10)

    def run_task(self, booking):
        return notify_waitlist(booking_id=str(booking.pk), organization_id=str(self.org.pk))

    def test_cancellation_queues_matching_after_commit(self, send):
        self.join()
        with patch("bookings.tasks.notify_waitlist.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                cancel_booking(booking=self.freed, reason="Sick")
        delay.assert_called_once_with(
            booking_id=str(self.freed.pk), organization_id=str(self.org.pk)
        )

    def test_rescheduling_offers_only_a_time_really_left(self, send):
        booking = self.freed
        with self.assertRaises(DomainError) as raised:
            reschedule_booking(booking=booking, new_start=booking.start_datetime)
        self.assertEqual(raised.exception.code, "same_time")
        with patch("bookings.tasks.notify_waitlist.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                moved = reschedule_booking(booking=booking, new_start=f.future(3, 10, 30))
            delay.assert_not_called()  # still overlaps the old time: nothing was freed
            with self.captureOnCommitCallbacks(execute=True):
                reschedule_booking(booking=moved, new_start=f.future(3, hour=15))
            delay.assert_called_once()

    def test_matching_entries_are_emailed_once_first_come_first_served(self, send):
        waiting = [self.join(f"w{index}@x.test") for index in range(NOTIFY_LIMIT + 2)]
        cancel_booking(booking=self.freed)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.run_task(self.freed), NOTIFY_LIMIT)
        notified = WaitlistEntry.objects.filter(status=WaitlistEntry.Status.NOTIFIED)
        self.assertEqual(
            set(notified.values_list("pk", flat=True)),
            {entry.pk for entry in waiting[:NOTIFY_LIMIT]},
        )
        self.assertEqual(send.call_count, NOTIFY_LIMIT)
        # A second freed time doesn't email the same people again.
        second = self.book(hour=15)
        cancel_booking(booking=second)
        self.assertEqual(
            [entry.customer_email for entry in notify_matching_entries(second)],
            [entry.customer_email for entry in waiting[NOTIFY_LIMIT:]],
        )

    def test_the_email_names_the_time_not_the_other_customer(self, send):
        entry = self.join()
        cancel_booking(booking=self.freed)
        with self.captureOnCommitCallbacks(execute=True):
            self.run_task(self.freed)
        with override_settings(SITE_URL="https://book.example"):
            send_templated_email.apply(kwargs=send.call_args.kwargs).get()
        (message,) = mail.outbox
        self.assertEqual(message.to, ["wait@x.test"])
        self.assertIn("Massage", message.body)
        self.assertIn("https://book.example/book/glow/", message.body)
        self.assertNotIn("Booked Person", message.body)
        self.assertNotIn(self.freed.reference, message.body)
        log = NotificationLog.objects.get(notification_type="waitlist_slot_available")
        self.assertEqual(log.related_waitlist_entry, entry)
        # The sent email is on the waiting customer's timeline, not the cancelled one's.
        activity = CustomerActivity.objects.get(kind=CustomerActivity.Kind.EMAIL_SENT)
        self.assertEqual(activity.customer, entry.customer)
        self.assertEqual(activity.metadata["reference"], "")

    def test_task_refuses_another_tenants_booking(self, send):
        self.join()
        cancel_booking(booking=self.freed)
        other = f.OrganizationFactory()
        self.assertEqual(
            notify_waitlist(booking_id=str(self.freed.pk), organization_id=str(other.pk)), 0
        )
        self.assertFalse(
            NotificationLog.objects.filter(notification_type="waitlist_slot_available").exists()
        )


class PublicJoinTests(Fixtures, TestCase):
    URL = "/book/glow/waitlist/"

    def setUp(self):
        self.make_fixtures()

    def test_join_from_the_booking_page(self):
        self.client.post("/book/glow/service/", {"service": str(self.service.pk)})
        self.assertContains(self.client.get("/book/glow/time/"), "Join the waitlist")
        response = self.client.post(self.URL, {**DETAILS, "time_of_day": "morning"})
        self.assertRedirects(response, "/book/glow/waitlist/joined/")
        entry = WaitlistEntry.objects.get()
        self.assertEqual(
            (entry.source, entry.preferred_staff, entry.time_of_day),
            (Booking.Source.PUBLIC_BOOKING, self.staff, "morning"),
        )
        self.assertTrue(entry.location.is_default)
        self.assertNotContains(self.client.get("/book/glow/waitlist/joined/"), "ada@example.test")

    def test_needs_a_service_first(self):
        self.assertRedirects(self.client.get(self.URL), "/book/glow/service/")

    def test_honeypot(self):
        self.client.post("/book/glow/service/", {"service": str(self.service.pk)})
        response = self.client.post(self.URL, {**DETAILS, "website": "spam", "time_of_day": "any"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(WaitlistEntry.objects.exists())

    @override_settings(PUBLIC_BOOKING_RATE="1/3600")
    def test_rate_limited(self):
        from django.core.cache import cache

        cache.clear()
        self.client.post("/book/glow/service/", {"service": str(self.service.pk)})
        self.client.post(self.URL, {**DETAILS, "time_of_day": "any"})
        response = self.client.post(
            self.URL, {**DETAILS, "email": "b@x.test", "time_of_day": "any"}
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(WaitlistEntry.objects.count(), 1)
        cache.clear()

    def test_nobody_can_list_entries_anonymously(self):
        self.join()
        response = APIClient().get("/api/v1/waitlist/", HTTP_X_ORGANIZATION_SLUG="glow")
        self.assertIn(response.status_code, (401, 403))


class ApiTests(Fixtures, TestCase):
    """The API writes through the waitlist service: no hand-set status, no generic edits."""

    def setUp(self):
        self.make_fixtures()
        self.client = APIClient()
        self.client.force_authenticate(
            f.MembershipFactory(organization=self.org, role=OrganizationRole.RECEPTIONIST).user
        )

    def post(self, url, payload=None):
        return self.client.post(url, payload, format="json", HTTP_X_ORGANIZATION_SLUG="glow")

    def test_create_goes_through_the_service(self):
        payload = {
            "service": str(self.service.pk),
            "customer_name": "Api Person",
            "customer_email": "api@x.test",
            "status": "notified",  # ignored: the workflow sets it
            "expires_at": "2000-01-01T00:00:00Z",  # ignored
        }
        response = self.post("/api/v1/waitlist/", payload)
        self.assertEqual(response.status_code, 201, response.content)
        entry = WaitlistEntry.objects.get()
        self.assertEqual(entry.status, WaitlistEntry.Status.WAITING)
        self.assertEqual(entry.source, Booking.Source.API)
        self.assertEqual(entry.customer.email, "api@x.test")
        self.assertGreater(entry.expires_at, timezone.now())
        again = self.post("/api/v1/waitlist/", payload | {"time_of_day": "evening"})
        self.assertEqual(again.json()["id"], str(entry.pk))
        self.assertEqual(WaitlistEntry.objects.count(), 1)

    def test_impossible_preferences_are_400(self):
        payload = {
            "service": str(self.service.pk),
            "customer_name": "Api Person",
            "customer_email": "api@x.test",
            "preferred_staff": str(f.StaffProfileFactory(organization=self.org).pk),
        }
        response = self.post("/api/v1/waitlist/", payload)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(WaitlistEntry.objects.exists())

    def test_no_generic_edits_and_close_through_the_service(self):
        entry = self.join()
        url = f"/api/v1/waitlist/{entry.pk}/"
        for method in ("put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    url, {"status": "notified"}, format="json", HTTP_X_ORGANIZATION_SLUG="glow"
                )
                self.assertEqual(response.status_code, 405)
        entry.refresh_from_db()
        self.assertEqual(entry.status, WaitlistEntry.Status.WAITING)
        response = self.post(f"{url}close/")
        self.assertEqual(response.status_code, 200, response.content)
        entry.refresh_from_db()
        self.assertEqual(entry.status, WaitlistEntry.Status.CLOSED)


@POSTGRES
class JoinRaceTests(Fixtures, TransactionTestCase):
    """Two joins for the same customer and service at the same moment: one entry."""

    def setUp(self):
        self.make_fixtures()
        self.customer = f.CustomerFactory(organization=self.org, email="wait@x.test")

    def test_concurrent_joins_make_one_entry(self):
        barrier = threading.Barrier(2, timeout=10)
        real = waitlist._waiting_entry
        local = threading.local()

        def both_miss(*args):
            if not getattr(local, "looked", False):
                local.looked = True
                barrier.wait()  # both have looked and found nothing
                return None
            return real(*args)

        def attempt(time_of_day):
            connections.close_all()
            try:
                return self.join(customer=self.customer, time_of_day=time_of_day).pk
            finally:
                connections.close_all()

        with (
            patch.object(waitlist, "_waiting_entry", both_miss),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(attempt, (TimeOfDay.MORNING, TimeOfDay.EVENING)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(WaitlistEntry.objects.count(), 1)


class ReceptionScreenTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user
        self.client.force_login(self.receptionist)

    def test_list_add_and_close(self):
        f.OrganizationFactory()  # another organization's entries never show
        self.join("listed@x.test")
        self.assertContains(self.client.get("/app/waitlist/"), "listed@x.test")
        response = self.client.post(
            "/app/waitlist/new/",
            {
                "name": "Walt Walker",
                "email": "walt@x.test",
                "service": str(self.service.pk),
                "time_of_day": "any",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204, response.content)
        entry = WaitlistEntry.objects.get(customer_email="walt@x.test")
        self.assertEqual(entry.source, Booking.Source.RECEPTION)
        self.client.post(f"/app/waitlist/{entry.pk}/close/")
        entry.refresh_from_db()
        self.assertEqual(entry.status, WaitlistEntry.Status.CLOSED)
        self.assertNotContains(self.client.get("/app/waitlist/"), "walt@x.test")

    def test_providers_are_refused(self):
        provider = f.MembershipFactory(organization=self.org, role=OrganizationRole.STAFF).user
        self.client.force_login(provider)
        self.assertEqual(self.client.get("/app/waitlist/").status_code, 403)

    def test_other_organizations_entry_is_404(self):
        other = f.WaitlistEntryFactory()
        self.assertEqual(self.client.post(f"/app/waitlist/{other.pk}/close/").status_code, 404)


class BackfillTests(Fixtures, TestCase):
    def test_existing_entries_are_linked_by_email(self):
        self.make_fixtures()
        customer = f.CustomerFactory(organization=self.org, email="Old@X.test")
        entry = f.WaitlistEntryFactory(
            organization=self.org, service=self.service, customer_email="old@x.test"
        )
        stranger = f.WaitlistEntryFactory(
            organization=self.org, service=self.service, customer_email="nobody@x.test"
        )
        migration = import_module("bookings.migrations.0013_backfill_waitlist_customers")
        migration.backfill(apps, None)
        entry.refresh_from_db()
        stranger.refresh_from_db()
        self.assertEqual(entry.customer, customer)
        self.assertIsNone(stranger.customer)
