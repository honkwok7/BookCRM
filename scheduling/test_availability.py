"""M3.4: the availability engine (scheduling/availability.py) and the public slot API."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from bookings.models import Booking
from core.exceptions import ConflictError, DomainError
from locations.models import Location
from locations.services import create_closure, create_location, set_location_hours
from organizations.models import OrganizationRole
from scheduling.availability import (
    QUERY_BUDGET,
    AvailabilityService,
    intersect,
    merge,
    subtract,
)
from scheduling.models import AvailabilityException, OrganizationHoliday, TimeOff
from services.services import create_service
from staff.services import add_offering, create_staff_profile
from tests import factories as f

TORONTO = ZoneInfo("America/Toronto")
MONDAY = date(2027, 3, 8)  # a week before the spring DST change
NOW = datetime(2027, 3, 1, 12, 0, tzinfo=UTC)


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=TORONTO)


def local_times(slots):
    return [slot.start.astimezone(TORONTO).strftime("%H:%M") for slot in slots]


class IntervalTests(TestCase):
    def test_merge_intersect_subtract(self):
        a, b, c, d, e = (at(MONDAY, hour) for hour in (9, 10, 11, 12, 13))
        self.assertEqual(merge([(c, d), (a, b), (b, c), (e, e)]), [(a, d)])
        self.assertEqual(intersect([(a, c)], [(b, d)]), [(b, c)])
        self.assertEqual(intersect([(a, b)], [(c, d)]), [])
        self.assertEqual(subtract([(a, e)], [(b, c)]), [(a, b), (c, e)])
        self.assertEqual(subtract([(a, c)], [(a, c)]), [])
        self.assertEqual(subtract([(b, d)], [(a, c), (c, e)]), [])


class EngineTestCase(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory(timezone="America/Toronto")
        self.main = Location.objects.get(organization=self.org)
        self.service = create_service(
            organization=self.org, name="Massage", duration_minutes=60, price=100
        )
        self.maya = self.provider()
        self.hours(self.maya, MONDAY.weekday(), 9, 17)

    def provider(self, locations=None, **fields):
        user = f.MembershipFactory(organization=self.org, role=OrganizationRole.STAFF).user
        staff = create_staff_profile(
            organization=self.org, user=user, locations=locations, **fields
        )
        add_offering(staff=staff, service=self.service)
        return staff

    def hours(self, staff, weekday, start, end, location=None):
        f.WeeklyAvailabilityFactory(
            organization=self.org,
            staff=staff,
            location=location,
            day_of_week=weekday,
            start_time=time(start),
            end_time=time(end),
        )

    def engine(self, **kwargs):
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("location", self.main)
        return AvailabilityService(self.org, kwargs.pop("service", self.service), **kwargs)

    def slots(self, day=MONDAY, staff=None, **kwargs):
        return self.engine(**kwargs).get_available_slots(day, day, staff=staff)

    def book(self, start, minutes=60, service=None, staff=None, **fields):
        return f.BookingFactory(
            organization=self.org,
            service=service or self.service,
            staff=staff or self.maya,
            start_datetime=start,
            end_datetime=start + timedelta(minutes=minutes),
            **fields,
        )


class SlotTests(EngineTestCase):
    def test_every_fifteen_minutes_within_hours(self):
        times = local_times(self.slots())
        self.assertEqual((times[0], times[-1], len(times)), ("09:00", "16:00", 29))
        self.assertEqual(self.slots(MONDAY + timedelta(days=1)), [])  # no hours on Tuesday

    def test_appointments_block_time_with_the_larger_buffer(self):
        buffered = create_service(
            organization=self.org,
            name="Hot stone",
            duration_minutes=60,
            price=120,
            buffer_after_minutes=15,
        )
        self.service.buffer_after_minutes = 10
        self.service.save()
        # The factory skips the booking service: give it the buffer snapshot the service takes.
        self.book(at(MONDAY, 11), service=buffered, buffer_after_minutes=15)  # 10:50-12:15
        times = local_times(self.slots())
        self.assertIn("09:45", times)
        self.assertNotIn("10:00", times)  # would end at 11:00, inside the 10-minute gap
        self.assertNotIn("12:00", times)
        self.assertIn("12:15", times)

    def test_cancelled_appointments_free_the_time(self):
        self.book(at(MONDAY, 11), status=Booking.Status.CANCELLED)
        self.assertIn("11:00", local_times(self.slots()))

    def test_location_opening_hours_limit_the_hours(self):
        set_location_hours(location=self.main, periods=[(MONDAY.weekday(), time(10), time(14))])
        times = local_times(self.slots())
        self.assertEqual((times[0], times[-1]), ("10:00", "13:00"))

    def test_closures_and_holidays(self):
        create_closure(
            location=self.main,
            start_date=MONDAY,
            all_day=False,
            start_time=time(12),
            end_time=time(13),
        )
        times = local_times(self.slots())
        self.assertIn("11:00", times)
        self.assertNotIn("11:15", times)
        self.assertNotIn("12:30", times)
        self.assertIn("13:00", times)
        OrganizationHoliday.objects.create(organization=self.org, date=MONDAY, name="Holiday")
        self.assertEqual(self.slots(), [])

    def test_all_day_closure_elsewhere_does_not_matter(self):
        downtown = create_location(organization=self.org, name="Downtown")
        create_closure(location=downtown, start_date=MONDAY)
        self.assertTrue(self.slots())
        create_closure(location=self.main, start_date=MONDAY - timedelta(days=1), end_date=MONDAY)
        self.assertEqual(self.slots(), [])

    def test_exceptions(self):
        AvailabilityException.objects.create(
            organization=self.org,
            staff=self.maya,
            date=MONDAY,
            unavailable_all_day=False,
            start_time=time(13),
            end_time=time(15),
        )
        self.assertEqual(local_times(self.slots()), ["13:00", "13:15", "13:30", "13:45", "14:00"])
        AvailabilityException.objects.create(
            organization=self.org, staff=self.maya, date=MONDAY, unavailable_all_day=True
        )
        self.assertEqual(self.slots(), [])

    def test_time_off(self):
        TimeOff.objects.create(
            organization=self.org,
            staff=self.maya,
            start_datetime=at(MONDAY, 12),
            end_datetime=at(MONDAY, 18),
            approval_status="pending",
        )
        self.assertIn("15:00", local_times(self.slots()))
        TimeOff.objects.filter(staff=self.maya).update(approval_status="approved")
        self.assertEqual(local_times(self.slots())[-1], "11:00")

    def test_daily_appointment_limit(self):
        self.maya.max_daily_appointments = 1
        self.maya.save()
        self.book(at(MONDAY, 9))
        self.assertEqual(self.slots(), [])

    def test_notice_and_booking_window_apply_to_the_public_only(self):
        now = at(MONDAY, 8, 30).astimezone(UTC)
        self.assertEqual(local_times(self.slots(now=now, public=True))[0], "09:30")
        self.assertEqual(local_times(self.slots(now=now, public=False))[0], "09:00")
        far = MONDAY + timedelta(days=35)  # beyond max_advance_days (30)
        self.hours(self.maya, far.weekday(), 9, 17)
        self.assertEqual(self.slots(far, now=now, public=True), [])
        self.assertTrue(self.slots(far, now=now, public=False))

    def test_hidden_providers_are_team_only(self):
        self.maya.online_booking_visible = False
        self.maya.save()
        self.assertEqual(self.slots(public=True), [])
        self.assertTrue(self.slots(public=False))

    def test_any_provider_lists_candidates_with_their_own_duration(self):
        daniel = self.provider()
        daniel.offerings.update(custom_duration_minutes=90)
        self.hours(daniel, MONDAY.weekday(), 9, 17)
        slots = {
            local: slot for local, slot in zip(local_times(self.slots()), self.slots(), strict=True)
        }
        self.assertEqual(set(slots["15:30"].staff), {self.maya, daniel})
        self.assertEqual(slots["15:45"].staff, [self.maya])  # Daniel would end after 17:00
        daniel_end = next(c.end for c in slots["15:30"].candidates if c.staff == daniel)
        self.assertEqual(daniel_end, at(MONDAY, 17))
        self.assertEqual(local_times(self.slots(staff=daniel))[-1], "15:30")

    def test_hours_can_be_tied_to_a_location(self):
        downtown = create_location(organization=self.org, name="Downtown")
        ines = self.provider(locations=[self.main, downtown])
        self.hours(ines, MONDAY.weekday(), 13, 15, location=downtown)
        self.assertEqual(self.slots(staff=ines), [])
        self.assertEqual(
            local_times(self.slots(staff=ines, location=downtown)),
            ["13:00", "13:15", "13:30", "13:45", "14:00"],
        )
        # Maya's hours (no location) apply anywhere she works; she doesn't work Downtown.
        self.assertEqual(self.slots(staff=self.maya, location=downtown), [])

    def test_service_not_offered_at_the_location(self):
        downtown = create_location(organization=self.org, name="Downtown")
        self.service.locations.set([downtown])
        self.assertEqual(self.slots(), [])


class DaylightSavingTests(EngineTestCase):
    def test_spring_forward_skips_the_missing_hour(self):
        sunday = date(2027, 3, 14)
        self.hours(self.maya, sunday.weekday(), 0, 5)
        times = local_times(self.slots(sunday, step_minutes=60))
        self.assertEqual(times, ["00:00", "01:00", "03:00", "04:00"])

    def test_fall_back_offers_the_repeated_hour(self):
        sunday = date(2027, 11, 7)
        self.hours(self.maya, sunday.weekday(), 0, 4)
        slots = self.slots(sunday, step_minutes=60)
        self.assertEqual(local_times(slots), ["00:00", "01:00", "01:00", "02:00", "03:00"])
        self.assertEqual(len({slot.start for slot in slots}), 5)  # five distinct instants


class ValidateSlotTests(EngineTestCase):
    def test_accepts_a_free_time_even_off_the_grid(self):
        candidate = self.engine().validate_slot(self.maya, at(MONDAY, 9, 7))
        self.assertEqual(candidate.end, at(MONDAY, 10, 7))

    def test_refusals(self):
        booked = self.book(at(MONDAY, 11))
        cases = [
            ({"start": at(MONDAY, 16, 30)}, "slot_unavailable"),  # would end after 17:00
            ({"start": at(MONDAY, 11, 30)}, "slot_unavailable"),
            ({"start": NOW - timedelta(hours=1)}, "in_past"),
            ({"start": at(MONDAY, 10), "staff": f.StaffProfileFactory()}, "not_offered"),
        ]
        for kwargs, code in cases:
            with self.subTest(code=code, start=kwargs["start"]):
                with self.assertRaises(DomainError) as raised:
                    self.engine().validate_slot(kwargs.get("staff", self.maya), kwargs["start"])
                self.assertEqual(raised.exception.code, code)
        with self.assertRaises(ConflictError):
            self.engine().validate_slot(self.maya, at(MONDAY, 11))
        # Rescheduling the same appointment may keep its own time.
        self.engine().validate_slot(self.maya, at(MONDAY, 11, 15), ignore_booking=booked)

    def test_public_notice(self):
        now = at(MONDAY, 8, 30).astimezone(UTC)
        with self.assertRaises(DomainError) as raised:
            self.engine(now=now, public=True).validate_slot(self.maya, at(MONDAY, 9))
        self.assertEqual(raised.exception.code, "too_soon")
        self.engine(now=now, public=False).validate_slot(self.maya, at(MONDAY, 9))


class QueryBudgetTests(EngineTestCase):
    def test_thirty_days_for_ten_providers(self):
        providers = [self.maya] + [self.provider() for _ in range(9)]
        for provider in providers:
            for weekday in range(5):
                self.hours(provider, weekday, 9, 17)
            self.book(at(MONDAY + timedelta(days=2), 10), staff=provider)
        TimeOff.objects.create(
            organization=self.org,
            staff=providers[1],
            start_datetime=at(MONDAY, 9),
            end_datetime=at(MONDAY, 12),
        )
        set_location_hours(location=self.main, periods=[(d, time(8), time(18)) for d in range(6)])
        create_closure(location=self.main, start_date=MONDAY + timedelta(days=5))
        with CaptureQueriesContext(connection) as queries:
            slots = self.engine().get_available_slots(MONDAY, MONDAY + timedelta(days=29))
        self.assertLessEqual(len(queries.captured_queries), QUERY_BUDGET)
        self.assertTrue(slots)
        self.assertEqual(len(slots[0].candidates), 9)  # providers[1] is off Monday morning


class PublicSlotApiTests(EngineTestCase):
    URL = "/api/v1/availability/slots/available-slots/"

    def setUp(self):
        super().setUp()
        self.org.slug = "glow"
        self.org.save()
        self.day = date.today() + timedelta(days=7)
        self.maya.weekly_availabilities.all().delete()
        self.hours(self.maya, self.day.weekday(), 9, 12)

    def get(self, **params):
        base = {
            "organization": "glow",
            "service": str(self.service.pk),
            "date": self.day.isoformat(),
        }
        return APIClient().get(self.URL, {**base, **params})

    def test_any_provider(self):
        daniel = self.provider()
        daniel.display_name = "Daniel"
        daniel.save()
        self.hours(daniel, self.day.weekday(), 9, 12)
        body = self.get().json()
        self.assertEqual(body["timezone"], "America/Toronto")
        self.assertEqual(body["location"], str(self.main.pk))
        self.assertTrue(body["slots"][0].startswith(f"{self.day.isoformat()}T09:00:00"))
        names = {staff["name"] for staff in body["availability"][0]["staff"]}
        self.assertIn("Daniel", names)
        self.assertNotIn("@", str(body))

    def test_one_provider_and_a_date_range(self):
        response = self.get(
            staff=str(self.maya.pk), end_date=(self.day + timedelta(days=6)).isoformat()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["slots"]), 9)  # 09:00-11:00, one day a week

    def test_bad_requests(self):
        too_long = (self.day + timedelta(days=31)).isoformat()
        self.assertEqual(self.get(end_date=too_long).status_code, 400)
        self.assertEqual(self.get(date="tomorrow").status_code, 400)
        self.assertEqual(self.get(location="nope").status_code, 400)
        foreign = Location.objects.get(organization=f.OrganizationFactory())
        self.assertEqual(self.get(location=str(foreign.pk)).status_code, 404)
        self.maya.online_booking_visible = False
        self.maya.save()
        self.assertEqual(self.get(staff=str(self.maya.pk)).status_code, 404)
