"""Calendar drag and drop (MoveView) and appointment details in the page dialog."""

import json
from datetime import UTC, datetime, time

from django.test import TestCase

from bookings.models import Booking
from bookings.services import change_booking_status, create_booking
from organizations.models import OrganizationRole
from tests import factories as f
from tests.test_appointment_screens import HTMX, Fixtures

Status = Booking.Status


class MoveTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.receptionist)
        self.booking = create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.maya,
            customer_name="Ada Lovelace",
            customer_email="ada@x.test",
            start_datetime=datetime.combine(self.day, time(9), tzinfo=UTC),
            notify=False,
        )
        self.url = f"/app/appointments/{self.booking.pk}/move/"

    def at(self, hour, minute=0, tz="UTC"):
        return {"start": f"{self.day.isoformat()}T{hour:02d}:{minute:02d}", "tz": tz}

    def test_a_drop_asks_for_confirmation(self):
        response = self.client.get(self.url, self.at(13, 15), **HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="modal-title"')
        self.assertContains(response, "1:15 PM – 2:15 PM")
        self.assertContains(response, ">Move</button>")
        self.assertNotContains(response, "<html")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Status.CONFIRMED)  # nothing changes on GET

    def test_confirming_moves_the_appointment(self):
        response = self.client.post(self.url, self.at(13, 15), **HTMX)
        self.assertEqual(response.status_code, 200, response.content)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Status.CANCELLED)
        moved = Booking.objects.get(rescheduled_from=self.booking)
        self.assertEqual(moved.start_datetime, datetime.combine(self.day, time(13, 15), tzinfo=UTC))
        self.assertEqual(moved.staff, self.maya)
        self.assertEqual(moved.status_history.get().source, Booking.Source.RECEPTION)
        triggers = json.loads(response["HX-Trigger"])
        self.assertIn("calendar-refresh", triggers)
        self.assertEqual(triggers["toast"]["message"], "Appointment moved.")
        self.assertIn("modal:close", json.loads(response["HX-Trigger-After-Swap"]))

    def test_a_time_outside_the_providers_hours_is_refused(self):
        response = self.client.post(self.url, self.at(6), **HTMX)
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "alert", status_code=422)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Status.CONFIRMED)
        self.assertFalse(Booking.objects.filter(rescheduled_from=self.booking).exists())

    def test_bad_input_is_explained(self):
        for params, message in (
            (self.at(13, tz="Mars/Olympus"), "Unknown time zone."),
            (self.at(13, tz=""), "Unknown time zone."),
            ({"start": "tomorrow", "tz": "UTC"}, "Choose a new time."),
            ({"start": "2030-02-30T10:00", "tz": "UTC"}, "Choose a new time."),
            ({"start": "2030-01-07T10:00+05:00", "tz": "UTC"}, "Choose a new time."),
        ):
            with self.subTest(params=params):
                response = self.client.post(self.url, params, **HTMX)
                self.assertContains(response, message, status_code=422)
        self.assertFalse(Booking.objects.filter(rescheduled_from=self.booking).exists())

    def test_a_time_skipped_by_daylight_saving_is_refused(self):
        # Toronto skips 02:00-03:00 on 10 March 2030.
        params = {"start": "2030-03-10T02:30", "tz": "America/Toronto"}
        response = self.client.get(self.url, params, **HTMX)
        self.assertContains(response, "the clocks change", status_code=422)
        self.assertNotContains(response, ">Move</button>", status_code=422)

    def test_finished_appointments_cannot_be_moved(self):
        f.make_current(self.booking)
        change_booking_status(booking=self.booking, new_status=Status.NO_SHOW)
        response = self.client.post(self.url, self.at(13), **HTMX)
        self.assertContains(response, "be moved any more", status_code=422)

    def test_without_javascript_it_is_a_page_and_a_redirect(self):
        page = self.client.get(self.url, self.at(13))
        self.assertContains(page, "<html")
        self.assertContains(page, "Move appointment?", count=0)  # the dialog heading only
        response = self.client.post(self.url, self.at(13))
        moved = Booking.objects.get(rescheduled_from=self.booking)
        self.assertRedirects(response, f"/app/appointments/{moved.pk}/")

    def test_moving_needs_appointments_manage(self):
        viewer = f.MembershipFactory(
            organization=self.org,
            role=OrganizationRole.RECEPTIONIST,
            revoked_permissions=["appointments.manage"],
        ).user
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(self.url, self.at(13), **HTMX).status_code, 403)

    def test_providers_move_only_their_own_and_other_organizations_are_invisible(self):
        sams = create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.sam,
            customer_name="Grace Hopper",
            customer_email="grace@x.test",
            start_datetime=datetime.combine(self.day, time(9), tzinfo=UTC),
            notify=False,
        )
        self.client.force_login(self.provider_user)
        response = self.client.post(f"/app/appointments/{sams.pk}/move/", self.at(13), **HTMX)
        self.assertEqual(response.status_code, 404)
        stranger = f.BookingFactory()
        self.client.force_login(self.receptionist)
        response = self.client.post(f"/app/appointments/{stranger.pk}/move/", self.at(13), **HTMX)
        self.assertEqual(response.status_code, 404)


class CalendarMarkupTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.booking = create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.maya,
            customer_name="Ada Lovelace",
            customer_email="ada@x.test",
            start_datetime=datetime.combine(self.day, time(9), tzinfo=UTC),
            notify=False,
        )

    def calendar(self, user, view="week"):
        self.client.force_login(user)
        return self.client.get("/app/calendar/", {"view": view, "date": self.day.isoformat()})

    def test_movable_appointments_carry_the_move_url(self):
        response = self.calendar(self.receptionist)
        self.assertContains(response, f'data-move-url="/app/appointments/{self.booking.pk}/move/"')
        self.assertContains(response, 'data-zone="UTC"')
        self.assertContains(response, f'data-date="{self.day.isoformat()}"')
        self.assertContains(response, "or drag it to a new time")
        self.assertNotContains(response, "calendar-panel")

    def test_day_view_keeps_appointments_in_their_providers_column(self):
        self.assertContains(self.calendar(self.receptionist, view="day"), "data-same-column")

    def test_finished_or_read_only_appointments_are_not_draggable(self):
        viewer = f.MembershipFactory(
            organization=self.org,
            role=OrganizationRole.RECEPTIONIST,
            revoked_permissions=["appointments.manage"],
        ).user
        response = self.calendar(viewer)
        self.assertContains(response, "Ada Lovelace")
        self.assertNotContains(response, "data-move-url")
        f.make_current(self.booking)
        change_booking_status(booking=self.booking, new_status=Status.NO_SHOW)
        self.assertNotContains(self.calendar(self.receptionist), "data-move-url")

    def test_month_view_opens_appointments_in_the_dialog(self):
        response = self.calendar(self.receptionist, view="month")
        self.assertContains(response, f'data-open-url="/app/appointments/{self.booking.pk}/"')
        self.assertNotContains(response, "data-move-url")

    def test_details_load_into_the_dialog(self):
        self.client.force_login(self.receptionist)
        response = self.client.get(f"/app/appointments/{self.booking.pk}/", **HTMX)
        self.assertContains(response, 'id="modal-title"')
        self.assertContains(response, 'hx-target="#modal-body"')
        self.assertContains(response, "Open as a page")
        page = self.client.get(f"/app/appointments/{self.booking.pk}/")
        self.assertContains(page, 'id="appointment-heading"')
        self.assertNotContains(page, "Open as a page")
