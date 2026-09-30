"""M4.1 + M4.2: one booking service for every caller, and no double booking at the database.

- The availability engine runs for every booking, team bookings included (probe P7): outside
  working hours, a provider who doesn't offer the service, a location they don't work at.
- The provider's own duration and price are used; location, source, creator and buffers are
  recorded.
- Idempotency keys, the plan's monthly limit, the cancellation deadline for customers.
- ``record_past_booking`` for history; only the booking service writes appointment times and
  statuses (a source scan).
- PostgreSQL: the ``booking_staff_no_overlap`` exclusion constraint, its friendly 409, the
  overlap pre-check and a lock-free race that only the constraint can stop.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from importlib import import_module
from io import StringIO
from pathlib import Path
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from bookings import services
from bookings.models import Booking
from bookings.services import (
    OVERLAP_CONSTRAINT,
    cancel_booking,
    create_booking,
    record_past_booking,
    reschedule_booking,
)
from core.exceptions import ConflictError, DomainError
from organizations.models import OrganizationRole
from scheduling.availability import AvailabilityService, Candidate
from subscriptions.models import Plan, Subscription
from tests import factories as f
from tests.wizard_helpers import DETAILS as WIZARD_DETAILS
from tests.wizard_helpers import book_through_wizard

Status = Booking.Status
POSTGRES = skipUnless(connection.vendor == "postgresql", "PostgreSQL exclusion constraint")


class Fixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(slug="glow")
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.service = f.ServiceFactory(
            organization=self.org,
            duration_minutes=60,
            price=80,
            is_public=True,
            min_notice_minutes=24 * 60,
            buffer_before_minutes=5,
            buffer_after_minutes=10,
            cancellation_deadline_hours=24,
        )
        self.staff = f.StaffProfileFactory(organization=self.org)
        # Weekdays and weekends alike, 09:00-17:00 UTC.
        f.make_bookable(self.staff, self.service, start=time(9), end=time(17))
        self.start = f.future(3, hour=10)

    def book(self, start=None, **kwargs):
        options = {
            "organization": self.org,
            "service": self.service,
            "staff_profile": self.staff,
            "customer_name": "Ada Lovelace",
            "customer_email": "ada@example.test",
            "start_datetime": start or self.start,
            "notify": False,
        }
        options.update(kwargs)
        return create_booking(**options)


class AvailabilityForEveryCallerTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_team_booking_outside_working_hours_is_refused(self):
        with self.assertRaises(ConflictError) as raised:
            self.book(f.future(3, hour=20))
        self.assertEqual(raised.exception.code, "slot_unavailable")
        self.assertFalse(Booking.objects.exists())

    def test_team_api_booking_outside_working_hours_is_409(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        response = client.post(
            "/api/v1/bookings/",
            {
                "service": str(self.service.pk),
                "staff": str(self.staff.pk),
                "start_datetime": f.future(3, hour=20).isoformat(),
                "customer_name": "Ada",
                "customer_email": "ada@example.test",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "slot_unavailable")

    def test_provider_who_does_not_offer_the_service_is_refused(self):
        other_service = f.ServiceFactory(organization=self.org)
        with self.assertRaises(DomainError) as raised:
            self.book(service=other_service)
        self.assertEqual(raised.exception.code, "not_offered")

    def test_location_the_provider_does_not_work_at_is_refused(self):
        elsewhere = f.LocationFactory(organization=self.org)
        with self.assertRaises(DomainError) as raised:
            self.book(location=elsewhere)
        self.assertEqual(raised.exception.code, "not_offered")

    def test_other_organizations_location_is_refused(self):
        with self.assertRaises(DomainError) as raised:
            self.book(location=f.LocationFactory())
        self.assertEqual(raised.exception.code, "not_found")

    def test_buffers_are_respected(self):
        self.book()  # 10:00-11:00, 10 minutes after
        with self.assertRaises(ConflictError):
            self.book(self.start + timedelta(minutes=65), customer_email="b@example.test")
        self.book(self.start + timedelta(minutes=70), customer_email="c@example.test")

    def test_online_rules_apply_only_to_public_bookings(self):
        soon = timezone.now().replace(hour=15, minute=0, second=0, microsecond=0)
        if soon <= timezone.now() + timedelta(minutes=5):
            soon += timedelta(days=1)
        with self.assertRaises(DomainError) as raised:
            self.book(soon, public=True, source=Booking.Source.PUBLIC_BOOKING)
        self.assertEqual(raised.exception.code, "too_soon")
        self.assertEqual(self.book(soon).status, Status.CONFIRMED)  # the team may


class RecordedDetailsTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_default_location_source_creator_and_buffers(self):
        booking = self.book(actor=self.owner, source=Booking.Source.RECEPTION)
        self.assertEqual(booking.location.is_default, True)
        self.assertEqual(booking.source, Booking.Source.RECEPTION)
        self.assertEqual(booking.created_by, self.owner)
        self.assertEqual((booking.buffer_before_minutes, booking.buffer_after_minutes), (5, 10))
        self.assertEqual(booking.reference[:8], f"SCH-{self.start.year}")

    def test_providers_own_duration_and_price(self):
        offering = self.staff.offerings.get(service=self.service)
        offering.custom_duration_minutes = 90
        offering.custom_price = 120
        offering.save()
        booking = self.book()
        self.assertEqual(booking.end_datetime - booking.start_datetime, timedelta(minutes=90))
        self.assertEqual(booking.duration_snapshot_minutes, 90)
        self.assertEqual(booking.price_snapshot, 120)

    def test_booking_at_a_second_location(self):
        downtown = f.LocationFactory(organization=self.org, timezone="UTC")
        self.staff.locations.add(downtown)
        booking = self.book(location=downtown)
        self.assertEqual(booking.location, downtown)

    def test_unknown_source_is_refused(self):
        with self.assertRaises(DomainError) as raised:
            self.book(source="carrier_pigeon")
        self.assertEqual(raised.exception.code, "invalid_source")

    def test_every_entry_point_records_its_source(self):
        public_page = book_through_wizard(
            self.client, self.org, self.service, start=self.start, details=WIZARD_DETAILS
        )
        self.assertEqual(public_page.status_code, 302, public_page.content)

        team = APIClient()
        team.force_authenticate(self.owner)
        customer = APIClient()
        customer.force_authenticate(f.UserFactory())
        payload = {
            "service": str(self.service.pk),
            "staff": str(self.staff.pk),
            "customer_name": "Ada",
            "customer_email": "ada@example.test",
        }
        for client, hour, extra in (
            (team, 12, {}),
            (customer, 14, {"HTTP_X_ORGANIZATION_SLUG": "glow"}),
        ):
            response = client.post(
                "/api/v1/bookings/",
                {**payload, "start_datetime": f.future(3, hour=hour).isoformat()},
                format="json",
                **extra,
            )
            self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            list(Booking.objects.order_by("start_datetime").values_list("source", flat=True)),
            [
                Booking.Source.PUBLIC_BOOKING,
                Booking.Source.API,
                Booking.Source.CUSTOMER_PORTAL,
            ],
        )


class IdempotencyTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_replay_returns_the_same_booking(self):
        first = self.book(idempotency_key="abc")
        again = self.book(idempotency_key="abc")
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Booking.objects.count(), 1)

    def test_same_key_for_a_different_request_is_refused(self):
        self.book(idempotency_key="abc")
        with self.assertRaises(ConflictError) as raised:
            self.book(self.start + timedelta(hours=2), idempotency_key="abc")
        self.assertEqual(raised.exception.code, "idempotency_key_reused")

    def test_keys_are_per_organization(self):
        self.book(idempotency_key="abc")
        other_org = f.OrganizationFactory()
        service = f.ServiceFactory(organization=other_org)
        staff = f.make_bookable(f.StaffProfileFactory(organization=other_org), service)
        create_booking(
            organization=other_org,
            service=service,
            staff_profile=staff,
            customer_name="Ada",
            customer_email="ada@example.test",
            start_datetime=self.start,
            idempotency_key="abc",
            notify=False,
        )
        self.assertEqual(Booking.objects.filter(idempotency_key="abc").count(), 2)

    def test_api_header_replay(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        payload = {
            "service": str(self.service.pk),
            "staff": str(self.staff.pk),
            "start_datetime": self.start.isoformat(),
            "customer_name": "Ada",
            "customer_email": "ada@example.test",
        }
        first = client.post("/api/v1/bookings/", payload, format="json", HTTP_IDEMPOTENCY_KEY="k1")
        second = client.post("/api/v1/bookings/", payload, format="json", HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(first.json()["id"], second.json()["id"])


class MonthlyLimitTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        plan = Plan.objects.create(
            name="Tiny", slug="tiny", monthly_price=0, yearly_price=0, maximum_monthly_bookings=1
        )
        Subscription.objects.create(organization=self.org, plan=plan)
        self.org.refresh_from_db()

    def test_limit_is_per_calendar_month(self):
        first = self.book()
        with self.assertRaises(ConflictError) as raised:
            self.book(self.start + timedelta(hours=2), customer_email="b@example.test")
        self.assertEqual(raised.exception.code, "plan_limit")
        # Bookings made last month don't count.
        Booking.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(days=40))
        self.book(self.start + timedelta(hours=2), customer_email="b@example.test")


class DeadlineAndRescheduleTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.customer_user = f.UserFactory(email="ada@example.test", email_verified=True)

    def soon_booking(self):
        """An appointment inside the 24-hour cancellation deadline."""
        start = timezone.now().replace(hour=15, minute=0, second=0, microsecond=0)
        if start <= timezone.now() + timedelta(minutes=5):
            start += timedelta(days=1)
        return self.book(start, customer_user=self.customer_user)

    def test_customer_cannot_cancel_inside_the_deadline(self):
        booking = self.soon_booking()
        with self.assertRaises(DomainError) as raised:
            cancel_booking(booking=booking, actor=self.customer_user, enforce_deadline=True)
        self.assertEqual(raised.exception.code, "past_cancellation_deadline")
        cancel_booking(booking=booking, actor=self.owner)  # the team can
        booking.refresh_from_db()
        self.assertEqual(booking.status, Status.CANCELLED)

    def test_customer_api_cancel_inside_the_deadline_is_refused(self):
        booking = self.soon_booking()
        client = APIClient()
        client.force_authenticate(self.customer_user)
        response = client.post(f"/api/v1/bookings/{booking.pk}/cancel/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["code"], "past_cancellation_deadline")
        team = APIClient()
        team.force_authenticate(self.owner)
        response = team.post(f"/api/v1/bookings/{booking.pk}/cancel/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

    def test_customer_reschedule_inside_the_deadline_is_refused(self):
        booking = self.soon_booking()
        with self.assertRaises(DomainError):
            reschedule_booking(booking=booking, new_start=self.start, public=True)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Status.CONFIRMED)

    def test_reschedule_keeps_location_and_source(self):
        downtown = f.LocationFactory(organization=self.org, timezone="UTC")
        self.staff.locations.add(downtown)
        booking = self.book(location=downtown, source=Booking.Source.RECEPTION)
        moved = reschedule_booking(booking=booking, new_start=self.start + timedelta(hours=3))
        self.assertEqual((moved.location, moved.source), (downtown, Booking.Source.RECEPTION))
        self.assertEqual(moved.rescheduled_from, booking)

    def test_refused_reschedule_changes_nothing(self):
        booking = self.book()
        with self.assertRaises(ConflictError):
            reschedule_booking(booking=booking, new_start=f.future(3, hour=20))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Status.CONFIRMED)
        self.assertEqual(Booking.objects.count(), 1)


class RecordPastBookingTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.customer = f.CustomerFactory(organization=self.org)

    def record(self, **kwargs):
        options = {
            "organization": self.org,
            "service": self.service,
            "staff_profile": self.staff,
            "customer": self.customer,
            "start_datetime": timezone.now() - timedelta(days=3),
            "status": Status.COMPLETED,
        }
        options.update(kwargs)
        return record_past_booking(**options)

    def test_records_history_with_a_final_status(self):
        booking = self.record()
        self.assertEqual(booking.source, Booking.Source.IMPORT)
        self.assertEqual(booking.status_history.get().new_status, Status.COMPLETED)

    def test_refuses_future_times_and_active_statuses(self):
        with self.assertRaises(DomainError):
            self.record(start_datetime=self.start)
        with self.assertRaises(DomainError):
            self.record(status=Status.CONFIRMED)


class OnlyTheBookingServiceWritesTests(TestCase):
    """Acceptance criterion of M4.1: appointment times and statuses change only in
    ``bookings/services.py``. A source scan, so a new shortcut fails CI."""

    WRITES = re.compile(
        # Creating booking rows (always sets their times and status).
        r"Booking\.objects[^\n]*?\.(create|bulk_create|get_or_create|update_or_create)\(|"
        # Bulk updates of times or status on a booking queryset.
        r"(Booking\.objects|bookings)[^\n]*?\.update\([^)]*\b(status|start_datetime|end_datetime)=|"
        # Assigning them on an instance.
        r"\bbooking\.(status|start_datetime|end_datetime)\s*=[^=]"
    )

    def test_no_other_module_writes_bookings(self):
        root = Path(settings.BASE_DIR)
        offenders = []
        for path in root.rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            if (
                relative.startswith((".venv/", "tests/", "node_modules/"))
                or "/migrations/" in relative
                or path.name.startswith("test")
                or relative == "bookings/services.py"
            ):
                continue
            text = path.read_text(encoding="utf-8")
            offenders += [f"{relative}: {m.group(0)}" for m in self.WRITES.finditer(text)]
        self.assertEqual(offenders, [])


@POSTGRES
class ExclusionConstraintTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def overlapping(self, state=Status.CONFIRMED, minutes=30):
        return f.BookingFactory(
            organization=self.org,
            service=self.service,
            staff=self.staff,
            start_datetime=self.start + timedelta(minutes=minutes),
            end_datetime=self.start + timedelta(minutes=minutes + 60),
            status=state,
        )

    def test_database_refuses_overlapping_active_bookings(self):
        self.book()
        with self.assertRaises(IntegrityError) as raised, transaction.atomic():
            self.overlapping()
        self.assertIn(OVERLAP_CONSTRAINT, str(raised.exception))

    def test_back_to_back_and_inactive_bookings_are_fine(self):
        self.book()
        self.overlapping(minutes=60)
        self.overlapping(state=Status.CANCELLED)
        self.overlapping(state=Status.NO_SHOW)

    def test_service_turns_the_violation_into_a_409(self):
        self.book()
        # Pretend the engine and the lock both missed it: the constraint still refuses.
        candidate = Candidate(self.staff, self.start + timedelta(minutes=90))
        with patch.object(AvailabilityService, "validate_slot", return_value=candidate):
            with self.assertRaises(ConflictError) as raised:
                self.book(self.start + timedelta(minutes=30), customer_email="b@example.test")
        self.assertEqual(raised.exception.code, "slot_unavailable")
        self.assertEqual(Booking.objects.count(), 1)

    def drop_constraint(self):
        # DDL is transactional in PostgreSQL: the test's rollback restores the constraint.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # no pending FK checks
            cursor.execute(f"ALTER TABLE bookings_booking DROP CONSTRAINT {OVERLAP_CONSTRAINT}")

    def test_overlap_check_and_migration_precheck(self):
        call_command("check_booking_overlaps", stdout=StringIO())
        self.book()
        self.drop_constraint()
        self.overlapping()
        with self.assertRaises(CommandError):
            call_command("check_booking_overlaps", stdout=StringIO())
        migration = import_module("bookings.migrations.0010_booking_no_overlap_constraint")
        with connection.schema_editor(atomic=False) as editor:
            with self.assertRaises(RuntimeError) as raised:
                migration.add_constraint(None, editor)
        self.assertIn("Overlapping active appointments", str(raised.exception))


@POSTGRES
class LockFreeRaceTests(Fixtures, TransactionTestCase):
    """Two connections, the staff lock disabled and both past the availability check at the
    same moment: only the exclusion constraint stands between them and a double booking."""

    def setUp(self):
        self.make_fixtures()

    def test_constraint_alone_prevents_the_double_booking(self):
        barrier = Barrier(2, timeout=10)
        real_offering_for = services.offering_for

        def both_validated(*args, **kwargs):
            barrier.wait()  # both requests have passed validate_slot
            return real_offering_for(*args, **kwargs)

        def attempt(minutes, email):
            connections.close_all()
            try:
                self.book(self.start + timedelta(minutes=minutes), customer_email=email)
                return "booked"
            except ConflictError as error:
                return error.code
            finally:
                connections.close_all()

        with (
            patch.object(services, "lock_staff", lambda staff_id: None),
            patch.object(services, "offering_for", both_validated),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = sorted(pool.map(attempt, (0, 30), ("a@example.test", "b@example.test")))
        self.assertEqual(results, ["booked", "slot_unavailable"])
        self.assertEqual(Booking.objects.filter(status=Status.CONFIRMED).count(), 1)
