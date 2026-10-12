"""A click on an empty calendar spot opens a new appointment there (NewAppointmentView)."""

import json
from datetime import UTC, datetime, time, timedelta

from django.test import TestCase

from bookings.models import Booking
from organizations.models import OrganizationRole
from tests import factories as f
from tests.test_appointment_screens import HTMX, Fixtures

URL = "/app/appointments/new/"


class PrefillTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.receptionist)
        self.prefill = {"date": self.day.isoformat(), "at": "14:00", "staff": str(self.sam.pk)}

    def test_the_clicked_spot_is_kept_until_a_service_is_chosen(self):
        response = self.client.get(URL, self.prefill, **HTMX)
        self.assertContains(response, 'id="modal-title"')
        self.assertContains(response, 'name="at" value="14:00"')
        self.assertContains(response, f'name="for_staff" value="{self.sam.pk}"')
        self.assertContains(response, f'name="date" value="{self.day.isoformat()}"')

    def test_choosing_a_service_preselects_the_provider_and_time(self):
        data = {
            "date": self.day.isoformat(),
            "at": "14:00",
            "for_staff": str(self.sam.pk),
            "from_calendar": "True",
            "service": str(self.service.pk),
            "refresh": "1",
        }
        response = self.client.post(URL, data, **HTMX)
        self.assertContains(response, f'<option value="{self.sam.pk}" selected>Sam</option>')
        start = datetime.combine(self.day, time(14), tzinfo=UTC).isoformat()
        self.assertContains(response, f'<option value="{start}" selected>2:00 PM</option>')

    def test_a_busy_or_closed_time_picks_the_next_free_one(self):
        data = {"at": "07:10", "service": str(self.service.pk), "refresh": "1"}
        response = self.client.post(URL, {**self.payload(), **data, "start": ""}, **HTMX)
        start = datetime.combine(self.day, time(9), tzinfo=UTC).isoformat()
        self.assertContains(response, f'<option value="{start}" selected>9:00 AM</option>')

    def test_booking_from_the_calendar_stays_on_the_calendar(self):
        response = self.client.post(URL, self.payload(from_calendar="True"), **HTMX)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(Booking.objects.filter(staff=self.maya, customer=self.customer).exists())
        triggers = json.loads(response["HX-Trigger"])
        self.assertIn("calendar-refresh", triggers)
        self.assertEqual(triggers["toast"]["message"], "Appointment booked.")
        self.assertIn("modal:close", json.loads(response["HX-Trigger-After-Swap"]))

    def test_elsewhere_booking_still_opens_the_appointment(self):
        response = self.client.post(URL, self.payload(), **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertTrue(response["HX-Redirect"].startswith("/app/appointments/"))


class CalendarClickTargetTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def calendar(self, user, view="week", day=None):
        self.client.force_login(user)
        day = day or self.day
        return self.client.get("/app/calendar/", {"view": view, "date": day.isoformat()})

    def test_bookers_get_the_new_appointment_url(self):
        response = self.calendar(self.receptionist)
        self.assertContains(response, 'data-new-url="/app/appointments/new/"')
        self.assertContains(response, "Click an empty time to book there.")
        day = self.calendar(self.receptionist, view="day")
        self.assertContains(day, f'data-staff="{self.maya.pk}"')
        month = self.calendar(self.receptionist, view="month")
        self.assertContains(month, 'data-new-url="/app/appointments/new/"')
        self.assertContains(month, "Click an empty day to book there.")

    def test_past_days_are_not_bookable(self):
        response = self.calendar(self.receptionist, day=self.day - timedelta(days=14))
        self.assertContains(response, "data-past")
        self.assertNotContains(self.calendar(self.receptionist, view="day"), "data-past")

    def test_read_only_members_cannot_book_from_the_calendar(self):
        viewer = f.MembershipFactory(
            organization=self.org,
            role=OrganizationRole.RECEPTIONIST,
            revoked_permissions=["appointments.manage"],
        ).user
        response = self.calendar(viewer)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "data-new-url")
