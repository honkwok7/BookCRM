"""Fixes from the second Phase 4 review: idempotent replays, merge/reschedule lock order,
the waitlist admin, advertising only publicly bookable times, claiming up to the limit, the
calendar on daylight-saving days, and the lane limit."""

import threading
import time as clock
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from unittest import mock, skipUnless
from zoneinfo import ZoneInfo

from django.contrib import admin
from django.db import connection, connections
from django.test import RequestFactory, TestCase, TransactionTestCase

from bookings import services
from bookings.calendar import MAX_LANES, PlacedEvent, assign_lanes, place
from bookings.models import Booking, WaitlistEntry
from bookings.services import cancel_booking, create_booking, reschedule_booking
from bookings.waitlist import join_waitlist, matching_entries, notify_matching_entries
from core.exceptions import ConflictError
from crm.services import merge_customers
from services.models import Service
from staff.models import StaffProfile
from tests import factories as f

POSTGRES = skipUnless(connection.vendor == "postgresql", "row locks across connections")
TORONTO = ZoneInfo("America/Toronto")


class Fixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(slug="glow", timezone="UTC")
        self.service = f.ServiceFactory(organization=self.org, is_public=True, duration_minutes=60)
        self.staff = f.StaffProfileFactory(organization=self.org)
        f.make_bookable(self.staff, self.service, start=time(8), end=time(20))

    def book(self, start=None, email="ada@x.test", **kwargs):
        options = {
            "organization": self.org,
            "service": self.service,
            "staff_profile": self.staff,
            "customer_name": "Ada",
            "customer_email": email,
            "start_datetime": start or f.future(3, hour=10),
            "notify": False,
        }
        options.update(kwargs)
        return create_booking(**options)


class IdempotencyReplayTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.victim = f.UserFactory(email="victim@x.test", email_verified=True)
        self.first = self.book(email="victim@x.test", actor=self.victim, idempotency_key="abc")

    def test_the_same_caller_and_request_get_the_replay(self):
        again = self.book(email="victim@x.test", actor=self.victim, idempotency_key="abc")
        self.assertEqual(again.pk, self.first.pk)

    def test_someone_else_reusing_the_key_gets_nothing(self):
        attacker = f.UserFactory(email="attacker@x.test", email_verified=True)
        for actor, email in (
            (attacker, "attacker@x.test"),  # another account
            (attacker, "victim@x.test"),  # another account, even with the same email
            (None, "victim@x.test"),  # nobody signed in
            (self.victim, "other@x.test"),  # the same account, another customer
        ):
            with self.subTest(actor=actor, email=email):
                with self.assertRaises(ConflictError) as raised:
                    self.book(email=email, actor=actor, idempotency_key="abc")
                self.assertEqual(raised.exception.code, "idempotency_key_reused")
        self.assertEqual(Booking.objects.count(), 1)


@POSTGRES
class MergeVersusRescheduleTests(Fixtures, TransactionTestCase):
    """A reschedule holds its appointment and then needs the customer (the new row's
    foreign key); a merge of that customer running meanwhile must wait, not deadlock."""

    def setUp(self):
        self.make_fixtures()
        self.target = f.CustomerFactory(organization=self.org, email="t@x.test")
        self.duplicate = f.CustomerFactory(organization=self.org, email="d@x.test")
        self.booking = self.book(email="d@x.test", customer=self.duplicate)

    def test_both_finish_and_the_new_appointment_ends_up_with_the_target(self):
        locked = threading.Event()
        real_lock_booking = services.lock_booking

        def lock_and_linger(booking):
            result = real_lock_booking(booking)
            locked.set()
            clock.sleep(1.5)  # let the merge start and reach its first lock
            return result

        def reschedule():
            connections.close_all()
            try:
                return reschedule_booking(
                    booking=Booking.objects.get(pk=self.booking.pk),
                    new_start=f.future(3, hour=15),
                ).pk
            finally:
                connections.close_all()

        def merge():
            connections.close_all()
            try:
                assert locked.wait(10)
                merge_customers(
                    target=type(self.target).objects.get(pk=self.target.pk),
                    duplicate=type(self.duplicate).objects.get(pk=self.duplicate.pk),
                )
                return "merged"
            finally:
                connections.close_all()

        with (
            mock.patch.object(services, "lock_booking", lock_and_linger),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            moved = pool.submit(reschedule)
            merged = pool.submit(merge)
            new_pk, outcome = moved.result(timeout=30), merged.result(timeout=30)
        self.assertEqual(outcome, "merged")
        self.assertEqual(Booking.objects.get(pk=new_pk).customer_id, self.target.pk)


class WaitlistAdminTests(TestCase):
    def test_waitlist_entries_are_read_only_in_the_admin(self):
        request = RequestFactory().get("/admin/")
        request.user = f.UserFactory(is_staff=True, is_superuser=True)
        model_admin = admin.site._registry[WaitlistEntry]
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))


class PubliclyBookableMatchingTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        join_waitlist(
            organization=self.org,
            service=self.service,
            customer_name="Wendy",
            customer_email="wait@x.test",
        )

    def freed(self, start):
        booking = self.book(start=start, email="team@x.test")
        return cancel_booking(booking=booking)

    def test_a_listed_time_is_offered(self):
        self.assertEqual(len(matching_entries(self.freed(f.future(3, hour=10)))), 1)

    def test_times_the_public_cannot_book_are_not_offered(self):
        off_grid = self.freed(f.future(3, hour=11, minute=7))  # the team may book off the grid
        self.assertEqual(matching_entries(off_grid), [])

        freed = self.freed(f.future(3, hour=13))
        Service.objects.filter(pk=self.service.pk).update(is_public=False)
        freed.service.refresh_from_db()
        self.assertEqual(matching_entries(freed), [])
        Service.objects.filter(pk=self.service.pk).update(is_public=True)
        freed.service.refresh_from_db()

        StaffProfile.objects.filter(pk=self.staff.pk).update(online_booking_visible=False)
        freed.staff.refresh_from_db()
        self.assertEqual(matching_entries(freed), [])


class ClaimUpToTheLimitTests(Fixtures, TestCase):
    def test_entries_claimed_elsewhere_are_replaced_by_the_next_ones(self):
        self.make_fixtures()
        entries = [
            join_waitlist(
                organization=self.org,
                service=self.service,
                customer_name=f"W{n}",
                customer_email=f"w{n}@x.test",
            )
            for n in range(10)
        ]
        freed = cancel_booking(booking=self.book())
        candidates = matching_entries(freed)
        # A concurrent run claimed the first five after this run listed them.
        WaitlistEntry.objects.filter(pk__in=[e.pk for e in entries[:5]]).update(
            status=WaitlistEntry.Status.NOTIFIED
        )
        with mock.patch("bookings.waitlist.matching_entries", return_value=candidates):
            notified = notify_matching_entries(freed)
        self.assertEqual({e.pk for e in notified}, {e.pk for e in entries[5:]})


def appointment(start, end):
    return SimpleNamespace(start_datetime=start, end_datetime=end)


class CalendarDaylightSavingTests(TestCase):
    def test_an_appointment_in_the_repeated_hour_is_shown_with_its_real_length(self):
        # 01:30 EDT to 01:30 EST on 2027-11-07: one real hour.
        start = datetime(2027, 11, 7, 5, 30, tzinfo=UTC)
        placed = place(
            appointment(start, start + timedelta(hours=1)), TORONTO, date(2027, 11, 7), 0, 24
        )
        self.assertIsNotNone(placed)
        self.assertEqual((placed.row, placed.span), (7, 4))  # the 01:30 row, four 15-minute rows

    def test_an_appointment_across_the_missing_hour_is_not_doubled(self):
        # 01:30 EST to 03:30 EDT on 2027-03-14: one real hour.
        start = datetime(2027, 3, 14, 6, 30, tzinfo=UTC)
        placed = place(
            appointment(start, start + timedelta(hours=1)), TORONTO, date(2027, 3, 14), 0, 24
        )
        self.assertEqual((placed.row, placed.span), (7, 4))

    def test_an_ordinary_day_is_unchanged(self):
        start = datetime(2027, 6, 1, 13, 0, tzinfo=UTC)  # 09:00 EDT
        placed = place(
            appointment(start, start + timedelta(minutes=45)), TORONTO, date(2027, 6, 1), 8, 18
        )
        self.assertEqual((placed.row, placed.span), (5, 3))


class LaneLimitTests(TestCase):
    def test_lanes_never_exceed_the_safelisted_classes(self):
        events = [PlacedEvent(None, None, None, 1, 8) for _ in range(MAX_LANES + 6)]
        lanes = assign_lanes(events)
        self.assertEqual(lanes, MAX_LANES)
        self.assertTrue(all(1 <= event.lane <= MAX_LANES for event in events))
