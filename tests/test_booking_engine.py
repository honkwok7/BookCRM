"""Booking engine rules ported from the legacy ``appointments`` app (M1.6).

- Interval rules: bookings are half-open ``[start, end)``; any overlap with an active booking of
  the same staff member is refused, back-to-back bookings are fine, terminal statuses free
  the slot.
- The lifecycle transition table, checked exhaustively.
- Lock order: the staff calendar row, then the booking row.
- PostgreSQL race tests: two real connections, one ordered after the other by a gated lock and
  an observed ``pg_blocking_pids`` wait. Exactly one side wins; the other gets a clean 409.

Since M4.1 ``create_booking`` also runs the availability engine (``validate_slot``); these
fixtures make the provider bookable around the clock so the interval rules are what's tested.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, local
from time import monotonic
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from bookings import services
from bookings.models import Booking
from bookings.services import (
    ALLOWED_TRANSITIONS,
    RESCHEDULABLE_STATUSES,
    change_booking_status,
    create_booking,
    reschedule_booking,
)
from core.exceptions import ConflictError, DomainError
from organizations.models import OrganizationRole
from tests import factories as f

Status = Booking.Status
TERMINAL = [Status.COMPLETED, Status.CANCELLED, Status.NO_SHOW, Status.REJECTED]


def base_time():
    """10:00 UTC a week from now: comfortably in the future, on a round minute."""
    return (timezone.now() + timedelta(days=7)).replace(hour=10, minute=0, second=0, microsecond=0)


class BookingFixtures:
    def make_fixtures(self):
        self.organization = f.OrganizationFactory()
        self.owner = f.MembershipFactory(
            organization=self.organization, role=OrganizationRole.OWNER
        ).user
        self.staff = f.StaffProfileFactory(organization=self.organization)
        self.service = f.ServiceFactory(organization=self.organization, duration_minutes=60)
        f.make_bookable(self.staff, self.service)
        self.base = base_time()

    def at(self, minutes):
        return self.base + timedelta(minutes=minutes)

    def book(self, minutes=0, staff=None, email="guest@example.test"):
        return create_booking(
            organization=self.organization,
            service=self.service,
            staff_profile=staff or self.staff,
            customer_name="Guest",
            customer_email=email,
            customer_phone="",
            start_datetime=self.at(minutes),
            notify=False,
        )

    def happening_now(self, state=Status.CONFIRMED):
        """An appointment that started five minutes ago (check-in, no-show and so on are only
        allowed around the appointment's time)."""
        return f.make_current(self.existing(0, state=state))

    def existing(self, minutes=0, state=Status.CONFIRMED):
        return f.BookingFactory(
            organization=self.organization,
            service=self.service,
            staff=self.staff,
            start_datetime=self.at(minutes),
            end_datetime=self.at(minutes + 60),
            status=state,
        )


class IntervalRuleTests(BookingFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.existing(0)  # 10:00-11:00

    def assert_refused(self, minutes):
        count = Booking.objects.count()
        with self.assertRaises(ConflictError) as raised:
            self.book(minutes)
        self.assertEqual(raised.exception.code, "slot_unavailable")
        self.assertEqual(Booking.objects.count(), count)

    def test_same_start_refused(self):
        self.assert_refused(0)

    def test_partial_overlap_at_beginning_refused(self):
        self.assert_refused(-30)

    def test_partial_overlap_at_end_refused(self):
        self.assert_refused(30)

    def test_new_interval_inside_existing_refused(self):
        self.service.duration_minutes = 30
        self.assert_refused(15)

    def test_new_interval_containing_existing_refused(self):
        self.service.duration_minutes = 120
        self.assert_refused(-30)

    def test_back_to_back_before_and_after_allowed(self):
        self.book(-60)
        self.book(60)
        self.assertEqual(Booking.objects.count(), 3)

    def test_terminal_statuses_free_the_slot(self):
        for state in TERMINAL:
            with self.subTest(status=state):
                Booking.objects.update(status=state)
                booking = self.book(0)
                booking.delete()
                Booking.objects.update(status=Status.CONFIRMED)

    def test_other_staff_member_is_not_blocked(self):
        other = f.make_bookable(f.StaffProfileFactory(organization=self.organization), self.service)
        self.book(0, staff=other)

    def test_past_start_refused(self):
        with self.assertRaises(DomainError) as raised:
            self.book(-60 * 24 * 8)
        self.assertEqual(raised.exception.code, "in_past")


class RescheduleTests(BookingFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_reschedule_may_overlap_its_own_old_interval(self):
        booking = self.book(0)
        moved = reschedule_booking(booking=booking, new_start=self.at(30))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Status.CANCELLED)
        self.assertEqual(moved.start_datetime, self.at(30))
        self.assertEqual(moved.rescheduled_from, booking)

    def test_failed_reschedule_leaves_original_intact(self):
        booking = self.book(0)
        self.existing(180)
        with self.assertRaises(ConflictError):
            reschedule_booking(booking=booking, new_start=self.at(200))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Status.CONFIRMED)
        self.assertEqual(booking.start_datetime, self.at(0))
        self.assertEqual(Booking.objects.count(), 2)

    def test_only_pending_and_confirmed_can_be_rescheduled(self):
        for state in Status.values:
            with self.subTest(status=state):
                booking = self.existing(0, state=state)
                if state in RESCHEDULABLE_STATUSES:
                    reschedule_booking(booking=booking, new_start=self.at(24 * 60))
                else:
                    with self.assertRaises(ConflictError):
                        reschedule_booking(booking=booking, new_start=self.at(24 * 60))
                Booking.objects.all().delete()


class TransitionTableTests(BookingFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_every_status_pair_follows_the_table(self):
        for old in Status.values:
            for new in Status.values:
                if new == old or new == Status.CANCELLED:
                    continue  # cancel is idempotent and has its own path
                with self.subTest(old=old, new=new):
                    booking = self.happening_now(state=old)
                    if new in ALLOWED_TRANSITIONS[old]:
                        change_booking_status(booking=booking, new_status=new)
                        booking.refresh_from_db()
                        self.assertEqual(booking.status, new)
                    else:
                        with self.assertRaises(ConflictError):
                            change_booking_status(booking=booking, new_status=new)
                        booking.refresh_from_db()
                        self.assertEqual(booking.status, old)
                    booking.delete()

    def test_terminal_statuses_have_no_way_out(self):
        for state in TERMINAL:
            self.assertEqual(ALLOWED_TRANSITIONS[state], frozenset())

    def test_every_status_is_in_the_table(self):
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(Status.values))


class LockOrderTests(BookingFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def record_locks(self):
        calls = []
        real_staff, real_booking = services.lock_staff, services.lock_booking

        def staff(pk):
            calls.append("staff")
            return real_staff(pk)

        def booking(obj):
            calls.append("booking")
            return real_booking(obj)

        return calls, patch.multiple(services, lock_staff=staff, lock_booking=booking)

    def test_create_locks_the_staff_calendar(self):
        calls, patches = self.record_locks()
        with patches:
            self.book(0)
        self.assertEqual(calls, ["staff"])

    def test_reschedule_locks_staff_before_booking(self):
        booking = self.book(0)
        calls, patches = self.record_locks()
        with patches:
            reschedule_booking(booking=booking, new_start=self.at(120))
        self.assertEqual(calls[:2], ["staff", "booking"])
        self.assertNotIn("booking", calls[2:])

    def test_status_change_locks_only_the_booking(self):
        booking = f.make_current(self.book(0))
        calls, patches = self.record_locks()
        with patches:
            change_booking_status(booking=booking, new_status=Status.CHECKED_IN)
        self.assertEqual(calls, ["booking"])


@skipUnless(
    connection.vendor == "postgresql",
    "Requires PostgreSQL row locks and pg_blocking_pids; SQLite cannot prove these races.",
)
class PostgreSQLBookingRaceTests(BookingFixtures, TransactionTestCase):
    """Separate connections, controlled ordering, and an observed database wait."""

    def setUp(self):
        self.make_fixtures()

    # -- operations (method, url, payload) -----------------------------------------------------

    def creation(self, minutes=0):
        return (
            "/api/v1/bookings/",
            {
                "service": str(self.service.pk),
                "staff": str(self.staff.pk),
                "start_datetime": self.at(minutes).isoformat(),
                "customer_name": "Racer",
                "customer_email": "racer@example.test",
            },
        )

    def reschedule(self, booking, minutes):
        return (
            f"/api/v1/bookings/{booking.pk}/reschedule/",
            {"start_datetime": self.at(minutes).isoformat()},
        )

    def status(self, booking, new_status):
        return f"/api/v1/bookings/{booking.pk}/update_status/", {"status": new_status}

    def cancel(self, booking):
        return f"/api/v1/bookings/{booking.pk}/cancel/", {}

    # -- harness -------------------------------------------------------------------------------

    def race(self, first_operation, second_operation, lock_name="lock_staff"):
        first_locked, second_entered, release = Event(), Event(), Event()
        context = local()
        pids = {}
        original = getattr(services, lock_name)

        def gated_lock(*args, **kwargs):
            if context.label == "second":
                second_entered.set()
                return original(*args, **kwargs)
            result = original(*args, **kwargs)
            first_locked.set()
            if not release.wait(10):
                raise AssertionError("Timed out releasing first transaction")
            return result

        def worker(label, operation):
            context.label = label
            connections.close_all()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[label] = cursor.fetchone()[0]
                client = APIClient()
                client.force_authenticate(type(self.owner).objects.get(pk=self.owner.pk))
                url, data = operation
                response = client.post(url, data, format="json")
                return response.status_code, response.json()
            finally:
                connections.close_all()

        with (
            patch.object(services, lock_name, gated_lock),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(worker, "first", first_operation)
            second = None
            try:
                self.assertTrue(first_locked.wait(10), "First request never acquired its lock")
                second = pool.submit(worker, "second", second_operation)
                self.assertTrue(second_entered.wait(10), "Second request never attempted the lock")
                deadline = monotonic() + 10
                while True:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_blocking_pids(%s)", [pids["second"]])
                        blockers = cursor.fetchone()[0]
                    if pids["first"] in blockers:
                        break
                    self.assertFalse(
                        second.done(), "Competing request did not wait for the database lock"
                    )
                    self.assertLess(monotonic(), deadline, "No PostgreSQL lock wait observed")
            finally:
                release.set()
            return first.result(timeout=10), second.result(timeout=10)

    def assert_second_conflicts(self, results, first_status):
        self.assertEqual(results[0][0], first_status, results)
        self.assertEqual(results[1][0], 409, results)
        self.assertIn("code", results[1][1])

    def active(self):
        return Booking.objects.filter(status__in=services.ACTIVE_BOOKING_STATUSES)

    # -- races ---------------------------------------------------------------------------------

    def test_same_slot_concurrent_creation(self):
        self.assert_second_conflicts(self.race(self.creation(), self.creation()), 201)
        self.assertEqual(Booking.objects.count(), 1)

    def test_overlapping_concurrent_creation(self):
        self.assert_second_conflicts(self.race(self.creation(), self.creation(30)), 201)
        self.assertEqual(Booking.objects.count(), 1)

    def test_two_reschedules_compete_for_destination(self):
        first, second = self.existing(-120), self.existing(240)
        results = self.race(self.reschedule(first, 60), self.reschedule(second, 90))
        self.assert_second_conflicts(results, 200)
        second.refresh_from_db()
        self.assertEqual(second.status, Status.CONFIRMED)
        self.assertEqual(second.start_datetime, self.at(240))
        self.assertEqual(self.active().filter(start_datetime=self.at(60)).count(), 1)

    def test_creation_wins_against_reschedule(self):
        booking = self.existing(240)
        self.assert_second_conflicts(self.race(self.creation(), self.reschedule(booking, 30)), 201)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Status.CONFIRMED)

    def test_reschedule_wins_against_creation(self):
        booking = self.existing(240)
        self.assert_second_conflicts(self.race(self.reschedule(booking, 0), self.creation(30)), 200)
        self.assertEqual(self.active().count(), 1)

    def test_status_race_cannot_overwrite_terminal_state(self):
        booking = self.happening_now(state=Status.CHECKED_IN)
        results = self.race(
            self.status(booking, Status.COMPLETED),
            self.status(booking, Status.NO_SHOW),
            lock_name="lock_booking",
        )
        self.assert_second_conflicts(results, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Status.COMPLETED)

    def test_cancellation_cannot_be_overwritten_by_reschedule(self):
        booking = self.existing(0)
        results = self.race(
            self.cancel(booking), self.reschedule(booking, 120), lock_name="lock_booking"
        )
        self.assert_second_conflicts(results, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Status.CANCELLED)
        self.assertEqual(self.active().count(), 0)

    def test_status_change_after_concurrent_reschedule_is_refused(self):
        booking = self.existing(0)
        results = self.race(
            self.reschedule(booking, 120),
            self.status(booking, Status.CHECKED_IN),
            lock_name="lock_booking",
        )
        self.assert_second_conflicts(results, 200)
        self.assertEqual(self.active().get().start_datetime, self.at(120))
