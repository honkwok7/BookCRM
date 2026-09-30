"""M4.3: the appointment lifecycle.

- Timing: check-in, start and completion from an hour before the start; no-show once it has
  started. Check-in and completion are timestamped.
- Every status change has a history row with its reason and source (the channel).
- API: check-in / check-out actions, the history endpoint, who may do what.
- The transition table itself is checked exhaustively in tests/test_booking_engine.py.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking
from bookings.services import (
    cancel_booking,
    change_booking_status,
    check_in,
    check_out,
    create_booking,
    reschedule_booking,
)
from core.exceptions import ConflictError, DomainError
from crm.models import CustomerActivity
from organizations.models import OrganizationRole
from tests import factories as f

Status = Booking.Status


class Fixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(slug="glow")
        self.service = f.ServiceFactory(organization=self.org, duration_minutes=60)
        self.provider_user = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.STAFF
        ).user
        self.staff = f.StaffProfileFactory(organization=self.org, user=self.provider_user)
        self.other_staff = f.StaffProfileFactory(organization=self.org)
        for staff in (self.staff, self.other_staff):
            f.make_bookable(staff, self.service)
        self.receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user

    def book(self, staff=None, days=2, **kwargs):
        return create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=staff or self.staff,
            customer_name="Ada Lovelace",
            customer_email="ada@example.test",
            start_datetime=f.future(days),
            notify=False,
            **kwargs,
        )

    def starting_in(self, minutes, **kwargs):
        booking = f.make_current(self.book(days=5, **kwargs))
        shift = timedelta(minutes=minutes + 5)  # make_current: started 5 minutes ago
        Booking.objects.filter(pk=booking.pk).update(
            start_datetime=booking.start_datetime + shift,
            end_datetime=booking.end_datetime + shift,
        )
        booking.refresh_from_db()
        return booking


class TimingTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_check_in_from_an_hour_before(self):
        with self.assertRaises(DomainError) as raised:
            check_in(booking=self.book())
        self.assertEqual(raised.exception.code, "too_early")
        booking = check_in(booking=self.starting_in(50))
        self.assertEqual(booking.status, Status.CHECKED_IN)
        self.assertIsNotNone(booking.checked_in_at)

    def test_no_show_only_once_started(self):
        booking = self.starting_in(10)
        with self.assertRaises(DomainError) as raised:
            change_booking_status(booking=booking, new_status=Status.NO_SHOW)
        self.assertEqual(raised.exception.code, "too_early")
        booking = f.make_current(booking)
        change_booking_status(booking=booking, new_status=Status.NO_SHOW)
        self.assertTrue(
            CustomerActivity.objects.filter(
                customer=booking.customer, kind=CustomerActivity.Kind.APPOINTMENT_NO_SHOW
            ).exists()
        )

    def test_check_out_completes_and_stamps(self):
        booking = check_in(booking=f.make_current(self.book()))
        booking = check_out(booking=booking)
        self.assertEqual(booking.status, Status.COMPLETED)
        self.assertIsNotNone(booking.completed_at)
        with self.assertRaises(ConflictError) as raised:
            check_out(booking=booking)
        self.assertEqual(raised.exception.code, "invalid_transition")


class HistoryTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def rows(self, booking):
        return list(
            booking.status_history.order_by("created_at").values_list(
                "old_status", "new_status", "reason", "source"
            )
        )

    def test_every_change_has_a_row_with_reason_and_source(self):
        booking = self.book(source=Booking.Source.RECEPTION)
        moved = reschedule_booking(
            booking=booking,
            new_start=booking.start_datetime + timedelta(hours=2),
            source=Booking.Source.RECEPTION,
        )
        self.assertEqual(
            self.rows(booking),
            [
                ("", Status.CONFIRMED, "", Booking.Source.RECEPTION),
                (Status.CONFIRMED, Status.CANCELLED, "Rescheduled", Booking.Source.RECEPTION),
            ],
        )
        self.assertEqual(
            self.rows(moved), [("", Status.CONFIRMED, "Rescheduled", Booking.Source.RECEPTION)]
        )
        cancel_booking(booking=moved, reason="Feeling unwell", source=Booking.Source.API)
        self.assertEqual(
            self.rows(moved)[-1],
            (Status.CONFIRMED, Status.CANCELLED, "Feeling unwell", Booking.Source.API),
        )

    def test_status_change_reason(self):
        booking = f.make_current(self.book())
        change_booking_status(
            booking=booking,
            new_status=Status.NO_SHOW,
            reason="Didn't answer the phone",
            note="Called twice",
        )
        row = booking.status_history.order_by("created_at").last()
        self.assertEqual((row.reason, row.note), ("Didn't answer the phone", "Called twice"))


class ApiTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.booking = f.make_current(self.book())

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_reception_checks_in_and_out(self):
        client = self.client_for(self.receptionist)
        url = f"/api/v1/bookings/{self.booking.pk}/"
        response = client.post(url + "check-in/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], Status.CHECKED_IN)
        self.assertIsNotNone(response.json()["checked_in_at"])
        self.assertEqual(client.post(url + "check-out/").json()["status"], Status.COMPLETED)
        history = client.get(url + "history/").json()
        self.assertEqual(
            [(row["new_status"], row["source"]) for row in history],
            [
                (Status.CONFIRMED, Booking.Source.STAFF),
                (Status.CHECKED_IN, Booking.Source.API),
                (Status.COMPLETED, Booking.Source.API),
            ],
        )

    def test_too_early_is_400(self):
        later = self.book(days=3)
        response = self.client_for(self.receptionist).post(f"/api/v1/bookings/{later.pk}/check-in/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "too_early")

    def test_providers_act_on_their_own_appointments_only(self):
        client = self.client_for(self.provider_user)
        self.assertEqual(
            client.post(f"/api/v1/bookings/{self.booking.pk}/check-in/").status_code, 200
        )
        theirs = f.make_current(self.book(staff=self.other_staff))
        self.assertEqual(client.post(f"/api/v1/bookings/{theirs.pk}/check-in/").status_code, 404)

    def test_customers_can_cancel_but_not_check_in_or_read_history(self):
        customer = f.UserFactory(email="ada@example.test", email_verified=True)
        client = self.client_for(customer)
        url = f"/api/v1/bookings/{self.booking.pk}/"
        self.assertEqual(client.post(url + "check-in/").status_code, 403)
        self.assertEqual(client.get(url + "history/").status_code, 403)
        # Inside the deadline a customer can't cancel online; a later appointment they can.
        later = self.book(days=4)
        response = client.post(f"/api/v1/bookings/{later.pk}/cancel/", {"reason": "Busy"})
        self.assertEqual(response.status_code, 200, response.content)
        row = later.status_history.order_by("created_at").last()
        self.assertEqual((row.reason, row.source), ("Busy", Booking.Source.CUSTOMER_PORTAL))

    def test_illegal_transition_message(self):
        change_booking_status(booking=self.booking, new_status=Status.NO_SHOW)
        response = self.client_for(self.receptionist).post(
            f"/api/v1/bookings/{self.booking.pk}/check-in/"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "invalid_transition")
        self.assertIn("no show", response.json()["detail"])

    def test_timestamps_are_not_set_before_the_change(self):
        self.assertIsNone(self.booking.checked_in_at)
        self.assertIsNone(self.booking.completed_at)
        self.assertLess(self.booking.start_datetime, timezone.now())
