"""Providers' working hours shaded on the calendar (scheduling/working_hours.py)."""

from datetime import UTC, date, datetime, time, timedelta

from django.test import TestCase

from locations.models import Location
from organizations.models import OrganizationRole
from scheduling.models import TimeOff
from scheduling.working_hours import working_hours
from tests import factories as f

MONDAY = date(2030, 1, 7)


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


class Fixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(timezone="UTC")
        self.main = Location.objects.get(organization=self.org, is_default=True)
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.maya = f.StaffProfileFactory(organization=self.org, display_name="Maya")
        self.sam = f.StaffProfileFactory(organization=self.org, display_name="Sam")
        for staff, start, end in ((self.maya, 9, 17), (self.sam, 12, 20)):
            for day in range(5):  # Monday to Friday
                f.WeeklyAvailabilityFactory(
                    organization=self.org,
                    staff=staff,
                    day_of_week=day,
                    start_time=time(start),
                    end_time=time(end),
                )


class WorkingHoursTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def hours(self, first=MONDAY, last=MONDAY, providers=None, **kwargs):
        return working_hours(self.org, providers or [self.maya], first, last, **kwargs)

    def test_weekly_hours(self):
        hours = self.hours(MONDAY, MONDAY + timedelta(days=6))
        self.assertEqual(hours[(self.maya.pk, MONDAY)], [(at(MONDAY, 9), at(MONDAY, 17))])
        self.assertNotIn((self.maya.pk, MONDAY + timedelta(days=5)), hours)  # Saturday

    def test_a_day_off_and_limited_hours(self):
        f.AvailabilityExceptionFactory(organization=self.org, staff=self.maya, date=MONDAY)
        self.assertEqual(self.hours(), {})
        tuesday = MONDAY + timedelta(days=1)
        f.AvailabilityExceptionFactory(
            organization=self.org,
            staff=self.maya,
            date=tuesday,
            unavailable_all_day=False,
            start_time=time(10),
            end_time=time(12),
        )
        self.assertEqual(
            self.hours(tuesday, tuesday)[(self.maya.pk, tuesday)],
            [(at(tuesday, 10), at(tuesday, 12))],
        )

    def test_time_off_is_cut_out(self):
        f.TimeOffFactory(
            organization=self.org,
            staff=self.maya,
            start_datetime=at(MONDAY, 12),
            end_datetime=at(MONDAY, 13),
            approval_status=TimeOff.ApprovalStatus.APPROVED,
        )
        f.TimeOffFactory(  # still pending: no effect
            organization=self.org,
            staff=self.maya,
            start_datetime=at(MONDAY, 15),
            end_datetime=at(MONDAY, 16),
            approval_status=TimeOff.ApprovalStatus.PENDING,
        )
        self.assertEqual(
            self.hours()[(self.maya.pk, MONDAY)],
            [(at(MONDAY, 9), at(MONDAY, 12)), (at(MONDAY, 13), at(MONDAY, 17))],
        )

    def test_closures_holidays_and_opening_hours(self):
        f.OrganizationHolidayFactory(organization=self.org, date=MONDAY)
        self.assertEqual(self.hours(), {})
        tuesday = MONDAY + timedelta(days=1)
        f.LocationClosureFactory(
            organization=self.org, location=self.main, start_date=tuesday, all_day=True
        )
        self.assertEqual(self.hours(tuesday, tuesday), {})
        wednesday = MONDAY + timedelta(days=2)
        f.LocationHoursFactory(
            organization=self.org,
            location=self.main,
            weekday=2,
            opens_at=time(10),
            closes_at=time(15),
        )
        self.assertEqual(
            self.hours(wednesday, wednesday)[(self.maya.pk, wednesday)],
            [(at(wednesday, 10), at(wednesday, 15))],
        )

    def test_rows_for_another_location_are_left_out_when_one_is_chosen(self):
        downtown = f.LocationFactory(organization=self.org, timezone="UTC")
        f.WeeklyAvailabilityFactory(
            organization=self.org,
            staff=self.maya,
            location=downtown,
            day_of_week=5,
            start_time=time(10),
            end_time=time(14),
        )
        saturday = MONDAY + timedelta(days=5)
        self.assertIn((self.maya.pk, saturday), self.hours(saturday, saturday))
        self.assertEqual(self.hours(saturday, saturday, location=self.main), {})

    def test_query_count_does_not_grow_with_providers_or_days(self):
        with self.assertNumQueries(7):
            working_hours(self.org, [self.maya, self.sam], MONDAY, MONDAY + timedelta(days=30))


class CalendarShadingTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.owner)

    def calendar(self, **params):
        return self.client.get("/app/calendar/", {"date": MONDAY.isoformat(), **params})

    def test_one_providers_week(self):
        response = self.calendar(view="week", staff=str(self.maya.pk))
        # The grid starts at 8:00 (rows of 15 minutes): 9:00-17:00 is row 5, 32 rows long.
        self.assertContains(response, "cal-open col-span-full row-start-5 row-span-32", count=5)
        self.assertContains(response, "Maya's working hours")

    def test_everyone_in_the_week_is_when_anyone_works(self):
        response = self.calendar(view="week")
        # Maya 9-17 and Sam 12-20 merge to 9:00-20:00: 44 rows.
        self.assertContains(response, "cal-open col-span-full row-start-5 row-span-44", count=5)
        self.assertContains(response, "when anyone ticked is working")

    def test_day_view_shades_each_providers_column(self):
        response = self.calendar(view="day")
        self.assertContains(response, "cal-open col-span-full row-start-5 row-span-32")  # Maya
        self.assertContains(response, "cal-open col-span-full row-start-17 row-span-32")  # Sam
        self.assertContains(response, "each provider's working hours")

    def test_month_view_has_no_shading(self):
        self.assertNotContains(self.calendar(view="month"), "cal-open")
