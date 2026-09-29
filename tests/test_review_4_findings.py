"""Regression tests for the fourth codebase review (Codex, 2026-09-29): findings F1-F7 on
M3.1 locations, M3.2 staff assignments and M3.3 services."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Event
from time import monotonic
from unittest import mock, skipUnless

from django.db import connection, connections
from django.db.migrations import RunPython
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.exceptions import ConflictError
from core.models import AuditLog
from locations.models import Location
from locations.services import create_closure, create_location, update_closure, update_location
from locations.tests import subscribe
from organizations.models import OrganizationRole
from services import services as service_layer
from services.models import Service, ServiceCategory
from services.services import create_category, create_service, update_service
from staff import services as staff_layer
from staff.models import StaffProfile
from staff.services import add_offering, create_staff_profile, update_staff_profile
from tests import factories as f


class F1CompoundApiWritesAreAtomicTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.client = APIClient()
        self.client.force_authenticate(
            f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        )

    def test_service_is_not_created_when_its_providers_are_refused(self):
        chiropractor = f.StaffProfileFactory(organization=self.org, provider_type="Chiropractor")
        response = self.client.post(
            "/api/v1/services/",
            {
                "name": "Physio",
                "duration_minutes": 45,
                "price": "90.00",
                "required_provider_type": "Physiotherapist",
                "assigned_staff_members": [str(chiropractor.pk)],
            },
            format="json",
        )
        self.assertEqual(
            (response.status_code, response.json()["code"]), (400, "provider_type_mismatch")
        )
        self.assertFalse(Service.objects.filter(name="Physio").exists())
        self.assertFalse(AuditLog.objects.filter(action="service.created").exists())

    def test_staff_profile_is_unchanged_when_its_locations_are_refused(self):
        staff = f.StaffProfileFactory(organization=self.org, job_title="Therapist")
        closed = create_location(organization=self.org, name="Closed")
        update_location(location=closed, is_active=False)
        response = self.client.patch(
            f"/api/v1/staff/{staff.pk}/",
            {"job_title": "Lead therapist", "locations": [str(closed.pk)]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        staff.refresh_from_db()
        self.assertEqual(staff.job_title, "Therapist")
        self.assertFalse(AuditLog.objects.filter(action="staff.updated").exists())


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class F2ParallelReactivationTests(TransactionTestCase):
    """Two reactivations at once with one place left: the second waits for the first's
    organization lock, then counts the first and is refused."""

    def test_only_one_of_two_parallel_reactivations_fits(self):
        org = f.OrganizationFactory()
        plan = subscribe(org, maximum_locations=1).plan
        plan.maximum_staff = 1
        plan.save()
        first_staff = f.StaffProfileFactory(organization=org, is_active=False)
        second_staff = f.StaffProfileFactory(organization=org, is_active=False)
        first_inside, release = Event(), Event()
        pids = {}
        original_check = staff_layer._check_plan_limit

        def gated_check(organization):
            if not first_inside.is_set():
                first_inside.set()
                if not release.wait(10):
                    raise AssertionError("Timed out releasing the first reactivation")
            return original_check(organization)

        def worker(label, staff):
            connections.close_all()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[label] = cursor.fetchone()[0]
                try:
                    update_staff_profile(staff=staff, is_active=True)
                    return "ok"
                except ConflictError as error:
                    return error.code
            finally:
                connections.close_all()

        with (
            mock.patch.object(staff_layer, "_check_plan_limit", gated_check),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(worker, "first", first_staff)
            try:
                self.assertTrue(first_inside.wait(10), "First reactivation never took the lock")
                second = pool.submit(worker, "second", second_staff)
                deadline = monotonic() + 10
                while True:
                    if "second" in pids:
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT pg_blocking_pids(%s)", [pids["second"]])
                            if pids["first"] in cursor.fetchone()[0]:
                                break
                    self.assertFalse(second.done(), "Second reactivation did not wait")
                    self.assertLess(monotonic(), deadline, "No lock wait observed")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10), "ok")
            self.assertEqual(second.result(timeout=10), "plan_limit")
        self.assertEqual(StaffProfile.objects.filter(organization=org, is_active=True).count(), 1)


class F3DataMigrationsDoNotDeleteOnReverseTests(TestCase):
    def test_reverse_is_a_no_op(self):
        from importlib import import_module

        for name in (
            "locations.migrations.0002_default_locations",
            "staff.migrations.0003_backfill_locations_and_offerings",
        ):
            with self.subTest(migration=name):
                (operation,) = import_module(name).Migration.operations
                self.assertIs(operation.reverse_code, RunPython.noop)


class F4PublicPathsRequireAnOfferingTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory(slug="glow", timezone="America/Toronto")
        self.massage = f.ServiceFactory(organization=self.org, name="Massage", is_public=True)
        self.chiro = f.ServiceFactory(organization=self.org, name="Chiropractic", is_public=True)
        self.therapist = f.StaffProfileFactory(organization=self.org, display_name="Maya")
        self.chiropractor = f.StaffProfileFactory(organization=self.org, display_name="Daniel")
        add_offering(staff=self.therapist, service=self.massage)
        add_offering(staff=self.chiropractor, service=self.chiro)
        self.start = timezone.localtime(timezone.now() + timedelta(days=7)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

    def test_public_page_refuses_a_provider_who_does_not_offer_the_service(self):
        response = self.client.post(
            "/book/glow/",
            {
                "service": str(self.massage.pk),
                "staff": str(self.chiropractor.pk),
                "start_datetime": self.start.strftime("%Y-%m-%dT%H:%M"),
                "customer_name": "Ada Lovelace",
                "customer_email": "ada@example.test",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Daniel doesn&#x27;t offer Massage", status_code=400)

    def test_slot_api_answers_like_a_missing_pair(self):
        url = "/api/v1/availability/slots/available-slots/"
        params = {
            "organization": "glow",
            "service": str(self.massage.pk),
            "staff": str(self.chiropractor.pk),
            "date": (date.today() + timedelta(days=7)).isoformat(),
        }
        self.assertEqual(APIClient().get(url, params).status_code, 404)
        params["staff"] = str(self.therapist.pk)
        self.assertEqual(APIClient().get(url, params).status_code, 200)

    def test_self_service_booking_api_refuses_the_pair(self):
        client = APIClient()
        client.force_authenticate(f.UserFactory())
        response = client.post(
            "/api/v1/bookings/",
            {
                "service": str(self.massage.pk),
                "staff": str(self.chiropractor.pk),
                "start_datetime": self.start.isoformat(),
                "customer_name": "Ada",
                "customer_email": "ada@example.test",
            },
            HTTP_X_ORGANIZATION_SLUG="glow",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("staff", response.json())


class F5MirrorShowsOnlyValidProvidersTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.main = Location.objects.get(organization=self.org)
        self.downtown = create_location(organization=self.org, name="Downtown")
        user = f.MembershipFactory(organization=self.org, role=OrganizationRole.STAFF).user
        self.alice = create_staff_profile(
            organization=self.org,
            user=user,
            provider_type="Massage therapist",
            locations=[self.main, self.downtown],
        )
        self.service = create_service(
            organization=self.org, name="Massage", duration_minutes=60, price=100
        )

    def mirror(self):
        return list(Service.objects.get(pk=self.service.pk).assigned_staff_members.all())

    def test_service_rule_changes_update_the_mirror(self):
        add_offering(staff=self.alice, service=self.service)
        self.assertEqual(self.mirror(), [self.alice])
        service = update_service(service=self.service, required_provider_type="Physiotherapist")
        self.assertEqual(self.mirror(), [])
        update_service(service=service, required_provider_type="massage therapist")
        self.assertEqual(self.mirror(), [self.alice])  # the kept offering counts again

    def test_service_locations_update_the_mirror(self):
        add_offering(staff=self.alice, service=self.service, location=self.downtown)
        update_service(service=self.service, locations=[self.main])
        self.assertEqual(self.mirror(), [])

    def test_provider_type_change_updates_the_mirror(self):
        service = update_service(service=self.service, required_provider_type="Massage therapist")
        add_offering(staff=self.alice, service=service)
        update_staff_profile(staff=self.alice, provider_type="Chiropractor")
        self.assertEqual(self.mirror(), [])


class F6CategoryUniqueRaceTests(TestCase):
    def test_a_lost_race_is_a_conflict_not_a_server_error(self):
        org = f.OrganizationFactory()
        create_category(organization=org, name="Body")
        # Simulate a parallel request that passed its checks before this row existed.
        with (
            mock.patch.object(service_layer, "_check_category", lambda category: None),
            mock.patch.object(service_layer, "_unique_slug", lambda *args, **kwargs: "body"),
        ):
            with self.assertRaises(ConflictError) as raised:
                create_category(organization=org, name="Body")
        self.assertEqual(raised.exception.code, "duplicate")
        self.assertEqual(ServiceCategory.objects.filter(organization=org).count(), 1)


class F7ClosureReasonIsRedactedTests(TestCase):
    def test_changed_reason_is_recorded_without_its_text(self):
        location = Location.objects.get(organization=f.OrganizationFactory())
        closure = create_closure(
            location=location, start_date=date.today() + timedelta(days=5), reason="Vacation"
        )
        update_closure(closure=closure, reason="Dr Smith medical leave")
        log = AuditLog.objects.get(action="location_closure.updated")
        self.assertEqual(log.metadata["changes"], {"reason": "changed"})
        self.assertNotIn("Smith", str(log.metadata))
