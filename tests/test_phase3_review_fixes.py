"""Fixes from the Phase 3 re-review: the availability engine (buffer snapshots, the public
grid, DST, long buffers, fair "anyone"), schedule input rules, the service/staff mirror,
offering reactivation, read-only admins, the wizard's service query and fixtures without a
default location."""

from datetime import UTC, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

from django.contrib import admin
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from bookings.services import create_booking
from bookings.wizard import Wizard
from core.exceptions import DomainError
from locations.models import Location, LocationClosure, LocationHours
from organizations.models import Organization, OrganizationRole
from scheduling.availability import AvailabilityService
from scheduling.models import AvailabilityException, OrganizationHoliday, TimeOff
from services.models import Service, ServiceCategory
from services.services import update_service
from staff.models import StaffProfile, StaffServiceOffering
from staff.selectors import list_providers_for, with_providers
from staff.services import set_service_providers, set_staff_locations, sync_service_mirrors
from tests import factories as f

TORONTO = ZoneInfo("America/Toronto")


class Fixtures:
    def make_fixtures(self, timezone="UTC"):
        self.org = f.OrganizationFactory(slug="glow", timezone=timezone)
        self.location = Location.objects.get(organization=self.org, is_default=True)
        self.service = f.ServiceFactory(
            organization=self.org, duration_minutes=60, is_public=True, min_notice_minutes=0
        )
        self.staff = f.StaffProfileFactory(organization=self.org)
        f.make_bookable(self.staff, self.service)

    def book(self, start, staff=None, email="a@x.test"):
        return create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=staff or self.staff,
            customer_name="A",
            customer_email=email,
            start_datetime=start,
            notify=False,
        )

    def engine(self, **kwargs):
        return AvailabilityService(self.org, self.service, **kwargs)


class BufferSnapshotTests(Fixtures, TestCase):
    def test_editing_the_service_keeps_existing_appointments_buffers(self):
        self.make_fixtures()
        Service.objects.filter(pk=self.service.pk).update(buffer_after_minutes=30)
        self.service.refresh_from_db()
        booking = self.book(f.future(3, hour=10))
        self.assertEqual(booking.buffer_after_minutes, 30)
        Service.objects.filter(pk=self.service.pk).update(buffer_after_minutes=0)
        self.service.refresh_from_db()
        day = booking.start_datetime.date()
        starts = {slot.start for slot in self.engine().get_available_slots(day, day)}
        self.assertNotIn(booking.end_datetime, starts)  # still 30 minutes after it
        self.assertIn(booking.end_datetime + timedelta(minutes=30), starts)


class PublicGridTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_the_public_can_only_book_listed_times(self):
        off_grid = f.future(3, hour=10, minute=7)
        with self.assertRaises(DomainError) as raised:
            self.engine(public=True).validate_slot(self.staff, off_grid)
        self.assertEqual(raised.exception.code, "not_on_grid")
        self.engine(public=True).validate_slot(self.staff, f.future(3, hour=10, minute=15))

    def test_the_team_may_book_any_free_time(self):
        self.engine().validate_slot(self.staff, f.future(3, hour=10, minute=7))


class DaylightSavingTests(Fixtures, TestCase):
    """Toronto falls back at 02:00 EDT on 2027-11-07: 01:00-02:00 happens twice."""

    def setUp(self):
        self.make_fixtures(timezone="America/Toronto")
        self.assertEqual(self.location.timezone, "America/Toronto")
        self.now = datetime(2027, 11, 1, tzinfo=UTC)

    def test_an_ambiguous_start_lasts_the_real_duration(self):
        start = datetime(2027, 11, 7, 1, 0, tzinfo=TORONTO)  # fold=0: the first 01:00 (EDT)
        candidate = self.engine(now=self.now).validate_slot(self.staff, start)
        self.assertEqual(candidate.end - start, timedelta(minutes=60))

    def test_rounding_in_the_repeated_hour_never_goes_back_in_time(self):
        engine = self.engine(now=self.now)
        moment = datetime(2027, 11, 7, 1, 5, tzinfo=TORONTO, fold=1).astimezone(UTC)  # EST
        rounded = engine._first_on_grid(moment)
        self.assertGreaterEqual(rounded, moment)
        self.assertEqual(rounded - moment, timedelta(minutes=10))  # 01:15 EST


class BufferLimitTests(Fixtures, TestCase):
    def test_buffers_are_at_most_a_day(self):
        self.make_fixtures()
        with self.assertRaises(DomainError) as raised:
            update_service(service=self.service, buffer_after_minutes=24 * 60 + 1)
        self.assertEqual(raised.exception.code, "invalid_buffer")
        update_service(service=self.service, buffer_after_minutes=24 * 60)


class AnyoneAvailableTests(Fixtures, TestCase):
    def test_the_least_busy_provider_comes_first(self):
        self.make_fixtures()
        busy = self.staff
        free = f.make_bookable(f.StaffProfileFactory(organization=self.org), self.service)
        self.book(f.future(3, hour=9), staff=busy)
        day = f.future(3).date()
        slot = next(
            slot
            for slot in self.engine().get_available_slots(day, day)
            if len(slot.candidates) == 2
        )
        self.assertEqual(slot.candidates[0].staff, free)


class ScheduleInputTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client = APIClient()
        self.client.force_authenticate(
            f.MembershipFactory(organization=self.org, role=OrganizationRole.MANAGER).user
        )

    def post(self, path, payload):
        return self.client.post(
            f"/api/v1/availability/{path}/", payload, format="json", HTTP_X_ORGANIZATION_SLUG="glow"
        )

    def test_malformed_records_are_refused(self):
        day = str(f.future(3).date())
        staff = str(self.staff.pk)
        cases = [
            ("exceptions", {"staff": staff, "date": day, "unavailable_all_day": False}),
            (
                "exceptions",
                {"staff": staff, "date": day, "start_time": "15:00", "end_time": "10:00"},
            ),
            (
                "time-off",
                {
                    "staff": staff,
                    "start_datetime": "2030-01-02T10:00:00Z",
                    "end_datetime": "2030-01-01T10:00:00Z",
                },
            ),
            ("holidays", {"date": day, "name": "Half", "full_day_closure": False}),
            (
                "holidays",
                {
                    "date": day,
                    "name": "Backwards",
                    "full_day_closure": False,
                    "start_time": "15:00",
                    "end_time": "10:00",
                },
            ),
        ]
        for path, payload in cases:
            with self.subTest(path=path, payload=payload):
                self.assertEqual(self.post(path, payload).status_code, 400)
        self.assertFalse(AvailabilityException.objects.exists())
        self.assertFalse(TimeOff.objects.exists())
        self.assertFalse(OrganizationHoliday.objects.exists())

    def test_valid_records_still_work(self):
        day = str(f.future(3).date())
        staff = str(self.staff.pk)
        ok = [
            ("exceptions", {"staff": staff, "date": day, "unavailable_all_day": True}),
            (
                "exceptions",
                {"staff": staff, "date": day, "start_time": "10:00", "end_time": "12:00"},
            ),
            ("holidays", {"date": day, "name": "Whole day"}),  # full-day closure by default
            (
                "holidays",
                {
                    "date": day,
                    "name": "Afternoon",
                    "full_day_closure": False,
                    "start_time": "13:00",
                    "end_time": "17:00",
                },
            ),
        ]
        for path, payload in ok:
            with self.subTest(path=path, payload=payload):
                response = self.post(path, payload)
                self.assertEqual(response.status_code, 201, response.content)


class MirrorTests(Fixtures, TestCase):
    def test_all_locations_offering_needs_a_shared_location(self):
        self.make_fixtures()
        annex = f.LocationFactory(organization=self.org)
        update_service(service=self.service, locations=[annex])
        sync_service_mirrors([self.service.pk])
        self.assertNotIn(self.staff, self.service.assigned_staff_members.all())
        self.assertEqual(list(list_providers_for(self.service, annex)), [])
        set_staff_locations(staff=self.staff, locations=[self.location, annex])
        self.assertIn(self.staff, self.service.assigned_staff_members.all())
        set_staff_locations(staff=self.staff, locations=[self.location])
        self.assertNotIn(self.staff, self.service.assigned_staff_members.all())

    def test_listing_a_provider_again_reactivates_their_offering(self):
        self.make_fixtures()
        StaffServiceOffering.objects.filter(staff=self.staff, service=self.service).update(
            is_active=False
        )
        sync_service_mirrors([self.service.pk])
        set_service_providers(service=self.service, staff_members=[self.staff])
        self.assertTrue(
            StaffServiceOffering.objects.get(staff=self.staff, service=self.service).is_active
        )
        self.assertIn(self.staff, self.service.assigned_staff_members.all())


class WizardServicesTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        hidden = f.StaffProfileFactory(organization=self.org, online_booking_visible=False)
        typed = f.StaffProfileFactory(organization=self.org, provider_type="Chiropractor")
        self.services = {
            "offered": self.service,
            "nobody": f.ServiceFactory(organization=self.org, is_public=True),
            "hidden_only": f.ServiceFactory(organization=self.org, is_public=True),
            "type_match": f.ServiceFactory(
                organization=self.org, is_public=True, required_provider_type="chiropractor"
            ),
            "type_mismatch": f.ServiceFactory(
                organization=self.org, is_public=True, required_provider_type="Dentist"
            ),
        }
        f.make_bookable(hidden, self.services["hidden_only"])
        f.make_bookable(typed, self.services["type_match"])
        StaffServiceOffering.objects.create(
            organization=self.org, staff=typed, service=self.services["type_mismatch"]
        )

    def test_matches_list_providers_for(self):
        expected = {
            service.pk
            for service in self.services.values()
            if list_providers_for(service, self.location, public=True).exists()
        }
        got = set(
            with_providers(
                Service.objects.filter(organization=self.org), self.location, public=True
            ).values_list("pk", flat=True)
        )
        self.assertEqual(got, expected)
        self.assertEqual(got, {self.services["offered"].pk, self.services["type_match"].pk})

    def test_one_query_whatever_the_number_of_services(self):
        wizard = Wizard(self.org, {}, location=self.location)
        with CaptureQueriesContext(connection) as queries:
            wizard.services()
        self.assertEqual(len(queries), 1)


class ReadOnlyAdminTests(TestCase):
    def test_phase_3_models_are_read_only_in_the_admin(self):
        request = RequestFactory().get("/admin/")
        request.user = f.UserFactory(is_staff=True, is_superuser=True)
        for model in (
            Location,
            LocationClosure,
            StaffProfile,
            Service,
            ServiceCategory,
            AvailabilityException,
            TimeOff,
            OrganizationHoliday,
        ):
            model_admin = admin.site._registry[model]
            with self.subTest(model=model.__name__):
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))
        self.assertNotIn(LocationHours, admin.site._registry)  # inline only, also read-only


class FixtureDefaultLocationTests(TestCase):
    def test_command_gives_fixture_organizations_a_default_location(self):
        now = datetime.now(UTC)  # a fixture carries its own timestamps
        organization = Organization(name="Loaded", slug="loaded", created_at=now, updated_at=now)
        organization.save_base(raw=True)  # as loaddata does: no signal
        self.assertFalse(Location.objects.filter(organization=organization).exists())
        call_command("ensure_default_locations", stdout=StringIO())
        self.assertTrue(
            Location.objects.filter(organization=organization, is_default=True).exists()
        )
        call_command("ensure_default_locations", stdout=StringIO())  # idempotent
        self.assertEqual(Location.objects.filter(organization=organization).count(), 1)
