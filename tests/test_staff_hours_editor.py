"""Editing a provider's weekly hours and special days (scheduling/hours.py, staff pages)."""

from datetime import UTC, datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from bookings.services import create_booking
from core.audit import AuditAction
from core.exceptions import DomainError
from core.models import AuditLog
from locations.models import Location
from organizations.models import OrganizationRole
from scheduling.hours import hours_conflicts, set_special_day, set_weekly_hours
from scheduling.models import AvailabilityException, WeeklyAvailability
from tests import factories as f

HTMX = {"HTTP_HX_REQUEST": "true"}


def week_data(days=(0, 1, 2, 3, 4), start="09:00", end="17:00", where="", **extra):
    data = {}
    for day in days:
        data |= {f"on_{day}": "on", f"where_{day}": where, f"start_{day}": start, f"end_{day}": end}
    return data | extra


class Fixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(timezone="UTC")
        self.main = Location.objects.get(organization=self.org, is_default=True)
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user
        self.service = f.ServiceFactory(organization=self.org, duration_minutes=60)
        self.daniel = f.StaffProfileFactory(organization=self.org, display_name="Daniel")
        f.make_bookable(self.daniel, self.service, start=time(9), end=time(17))
        self.url = f"/app/staff/{self.daniel.pk}/availability/edit/"


class WeeklyHoursServiceTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def rows(self):
        return list(
            WeeklyAvailability.objects.filter(staff=self.daniel)
            .order_by("day_of_week", "start_time")
            .values_list("day_of_week", "start_time", "end_time", "location")
        )

    def test_replaces_the_whole_week_and_audits(self):
        set_weekly_hours(
            staff=self.daniel,
            periods=[(0, time(10), time(12), None), (0, time(13), time(18), self.main)],
            actor=self.owner,
        )
        self.assertEqual(
            self.rows(), [(0, time(10), time(12), None), (0, time(13), time(18), self.main.pk)]
        )
        entry = AuditLog.objects.get(action=AuditAction.AVAILABILITY_UPDATED)
        before, after = entry.metadata["changes"]["weekly_hours"]
        self.assertIn("Mon 09:00-17:00", before)
        self.assertEqual(after, ["Mon 10:00-12:00", "Mon 13:00-18:00 Main"])

    def test_bad_hours_are_refused(self):
        other = f.LocationFactory(organization=self.org)
        for periods, code in (
            ([(0, time(12), time(9), None)], "invalid_hours"),
            ([(7, time(9), time(12), None)], "invalid_weekday"),
            ([(0, time(9), time(12), None), (0, time(11), time(13), None)], "overlapping"),
            ([(0, time(9), time(12), other)], "invalid_location"),
        ):
            with self.subTest(code=code), self.assertRaises(DomainError) as caught:
                set_weekly_hours(staff=self.daniel, periods=periods)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(self.rows()), 7)  # unchanged

    def test_conflicts_are_upcoming_appointments_outside_the_new_hours(self):
        day = f.future(3).date()
        start = datetime.combine(day, time(15), tzinfo=UTC)
        booking = create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.daniel,
            customer_name="Ada Lovelace",
            customer_email="ada@x.test",
            start_datetime=start,
            notify=False,
        )
        morning = [(d, time(9), time(12), None) for d in range(7)]
        self.assertEqual(hours_conflicts(self.daniel, morning), [booking])
        all_day = [(d, time(9), time(17), None) for d in range(7)]
        self.assertEqual(hours_conflicts(self.daniel, all_day), [])


class SpecialDayServiceTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.day = timezone.localdate() + timedelta(days=5)

    def test_one_per_date_and_audited(self):
        set_special_day(staff=self.daniel, day=self.day, off_all_day=True, actor=self.owner)
        set_special_day(
            staff=self.daniel,
            day=self.day,
            off_all_day=False,
            start=time(10),
            end=time(14),
            reason="Short day",
            actor=self.owner,
        )
        entry = AvailabilityException.objects.get(staff=self.daniel, date=self.day)
        self.assertEqual((entry.unavailable_all_day, entry.start_time), (False, time(10)))
        self.assertEqual(
            AuditLog.objects.filter(action=AuditAction.AVAILABILITY_EXCEPTION_CREATED).count(), 2
        )

    def test_past_days_and_bad_hours_are_refused(self):
        with self.assertRaises(DomainError):
            set_special_day(
                staff=self.daniel, day=timezone.localdate() - timedelta(days=1), off_all_day=True
            )
        with self.assertRaises(DomainError):
            set_special_day(
                staff=self.daniel, day=self.day, off_all_day=False, start=time(14), end=time(10)
            )


class HoursEditorPageTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.owner)

    def test_tab_offers_editing_to_managers_only(self):
        tab = f"/app/staff/{self.daniel.pk}/availability/"
        self.assertContains(self.client.get(tab), "Edit weekly hours")
        self.client.force_login(self.receptionist)
        response = self.client.get(tab)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Edit weekly hours")
        self.assertNotContains(response, "Add a special day")
        self.assertEqual(self.client.post(self.url, week_data()).status_code, 403)

    def test_form_shows_the_current_hours(self):
        response = self.client.get(self.url, **HTMX)
        self.assertContains(response, 'id="modal-title"')
        self.assertContains(response, 'name="start_0" value="09:00"')
        self.assertContains(response, 'name="on_6" id="id_on_6" checked')

    def test_save(self):
        response = self.client.post(self.url, week_data(start="10:00", end="18:00"), **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], f"/app/staff/{self.daniel.pk}/availability/")
        rows = WeeklyAvailability.objects.filter(staff=self.daniel)
        self.assertEqual(rows.count(), 5)
        self.assertEqual({(row.start_time, row.end_time) for row in rows}, {(time(10), time(18))})

    def test_second_period_and_validation(self):
        data = week_data(days=[0], start2_0="13:00", end2_0="17:00", end_0="12:00")
        self.assertEqual(self.client.post(self.url, data, **HTMX).status_code, 204)
        self.assertEqual(WeeklyAvailability.objects.filter(staff=self.daniel).count(), 2)
        bad = week_data(days=[0], start2_0="11:00", end2_0="17:00", end_0="12:00")
        response = self.client.post(self.url, bad, **HTMX)
        self.assertContains(response, "must start after the first ends", status_code=422)

    def test_copy_monday_fills_the_weekdays_without_saving(self):
        data = week_data(days=[0], start="07:30", end="15:30")
        response = self.client.post(self.url, data | {"copy_monday": "1"}, **HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="start_4" value="07:30"')
        self.assertNotContains(response, 'name="start_5" value="07:30"')  # Saturday untouched
        self.assertTrue(
            WeeklyAvailability.objects.filter(staff=self.daniel, start_time=time(9)).exists()
        )

    def test_appointments_outside_new_hours_need_confirming(self):
        day = f.future(3).date()
        create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.daniel,
            customer_name="Ada Lovelace",
            customer_email="ada@x.test",
            start_datetime=datetime.combine(day, time(15), tzinfo=UTC),
            notify=False,
        )
        data = week_data(days=range(7), start="09:00", end="12:00")
        response = self.client.post(self.url, data, **HTMX)
        self.assertContains(response, "1 upcoming appointment would fall outside", status_code=422)
        self.assertContains(response, "Ada Lovelace", status_code=422)
        self.assertContains(response, "Save anyway", status_code=422)
        self.assertFalse(
            WeeklyAvailability.objects.filter(staff=self.daniel, end_time=time(12)).exists()
        )
        response = self.client.post(self.url, data | {"confirm": "1"}, **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            WeeklyAvailability.objects.filter(staff=self.daniel, end_time=time(12)).count(), 7
        )

    def test_other_organizations_staff_are_404(self):
        stranger = f.StaffProfileFactory()
        url = f"/app/staff/{stranger.pk}/availability/edit/"
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_special_days(self):
        day = timezone.localdate() + timedelta(days=4)
        url = f"/app/staff/{self.daniel.pk}/availability/days/new/"
        response = self.client.post(
            url,
            {"day": day.isoformat(), "kind": "hours", "start": "10:00", "end": "14:00"},
            **HTMX,
        )
        self.assertEqual(response.status_code, 204)
        tab = self.client.get(f"/app/staff/{self.daniel.pk}/availability/")
        self.assertContains(tab, "Only 10:00–14:00")
        entry = AvailabilityException.objects.get(staff=self.daniel)
        response = self.client.post(
            f"/app/staff/{self.daniel.pk}/availability/days/{entry.pk}/delete/", **HTMX
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(AvailabilityException.objects.filter(staff=self.daniel).exists())

    def test_special_day_validation(self):
        url = f"/app/staff/{self.daniel.pk}/availability/days/new/"
        past = (timezone.localdate() - timedelta(days=1)).isoformat()
        response = self.client.post(url, {"day": past, "kind": "off"}, **HTMX)
        self.assertContains(response, "already passed", status_code=422)
        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        response = self.client.post(url, {"day": future, "kind": "hours"}, **HTMX)
        self.assertContains(response, "Enter the hours", status_code=422)
