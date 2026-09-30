"""M4.4: the calendar (/app/calendar/), the appointment panel and actions, the events feed."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from bookings.calendar import (
    PlacedEvent,
    assign_lanes,
    hour_bounds,
    month_grid,
    place,
)
from bookings.models import Booking
from organizations.models import OrganizationRole
from tests import factories as f

UTC = ZoneInfo("UTC")
Status = Booking.Status


class LayoutTests(TestCase):
    def booking(self, start, minutes=60):
        return Booking(start_datetime=start, end_datetime=start + timedelta(minutes=minutes))

    def test_rows_are_quarter_hours_from_the_first_hour(self):
        day = date(2030, 1, 7)
        event = place(self.booking(datetime(2030, 1, 7, 10, 0, tzinfo=UTC)), UTC, day, 8, 20)
        self.assertEqual((event.row, event.span), (9, 4))  # 10:00 is 2h = 8 rows after 8:00
        odd = place(self.booking(datetime(2030, 1, 7, 10, 10, tzinfo=UTC), 20), UTC, day, 8, 20)
        self.assertEqual((odd.row, odd.span), (9, 2))  # snaps outwards: 10:00-10:30

    def test_events_are_clipped_to_the_day(self):
        day = date(2030, 1, 7)
        late = self.booking(datetime(2030, 1, 7, 23, 0, tzinfo=UTC), 120)
        self.assertEqual(place(late, UTC, day, 0, 24).span, 4)
        self.assertIsNone(place(late, UTC, day + timedelta(days=2), 0, 24))

    def test_overlaps_get_lanes(self):
        events = [
            PlacedEvent(None, None, None, row, span) for row, span in ((1, 4), (3, 4), (5, 2))
        ]
        self.assertEqual(assign_lanes(events), 2)
        self.assertEqual([event.lane for event in events], [1, 2, 1])

    def test_hours_widen_to_fit(self):
        start = datetime(2030, 1, 7, 6, 30, tzinfo=UTC)
        self.assertEqual(hour_bounds([(start, start + timedelta(hours=15))]), (6, 22))
        self.assertEqual(hour_bounds([]), (8, 20))

    def test_month_grid(self):
        bookings = [self.booking(datetime(2030, 1, 7, 10, tzinfo=UTC)) for _ in range(5)]
        for booking in bookings:
            booking.staff_id = 1
        weeks = month_grid(bookings, UTC, 2030, 1, date(2030, 1, 1))
        cell = next(day for week in weeks for day in week if day.date == date(2030, 1, 7))
        self.assertEqual((len(cell.shown), cell.more), (3, 2))


class CalendarFixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(timezone="UTC")
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user
        self.provider_user = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.STAFF
        ).user
        self.maya = f.StaffProfileFactory(
            organization=self.org, user=self.provider_user, display_name="Maya"
        )
        self.sam = f.StaffProfileFactory(organization=self.org, display_name="Sam")
        self.service = f.ServiceFactory(organization=self.org, name="Massage")
        self.day = timezone.now().date() + timedelta(days=1)

    def appointment(self, staff, hour=10, **kwargs):
        start = datetime.combine(self.day, time(hour), tzinfo=UTC)
        return f.BookingFactory(
            organization=self.org,
            service=self.service,
            staff=staff,
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            **kwargs,
        )

    def get(self, user, **params):
        self.client.force_login(user)
        return self.client.get("/app/calendar/", params)


class CalendarPageTests(CalendarFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.mine = self.appointment(self.maya, customer_name="Ada Lovelace")
        self.theirs = self.appointment(self.sam, customer_name="Grace Hopper")
        self.cancelled = self.appointment(
            self.sam, hour=14, customer_name="Cancelled Carl", status=Status.CANCELLED
        )
        other_org = f.BookingFactory(customer_name="Outsider Olga")
        self.other_org = other_org

    def test_week_shows_the_organizations_active_appointments(self):
        response = self.get(self.owner, view="week", date=self.day.isoformat())
        self.assertContains(response, "Ada Lovelace")
        self.assertContains(response, "Grace Hopper")
        self.assertNotContains(response, "Cancelled Carl")
        self.assertNotContains(response, "Outsider Olga")

    def test_cancelled_on_request(self):
        response = self.get(self.owner, date=self.day.isoformat(), status="cancelled")
        self.assertContains(response, "Cancelled Carl")
        self.assertNotContains(response, "Ada Lovelace")

    def test_filters(self):
        response = self.get(self.owner, date=self.day.isoformat(), staff=str(self.sam.pk))
        self.assertNotContains(response, "Ada Lovelace")
        response = self.get(self.owner, date=self.day.isoformat(), staff="not-a-uuid")
        self.assertContains(response, "Ada Lovelace")  # ignored, not an error

    def test_providers_see_only_their_own(self):
        response = self.get(self.provider_user, view="day", date=self.day.isoformat())
        self.assertContains(response, "Ada Lovelace")
        self.assertNotContains(response, "Grace Hopper")
        response = self.get(self.provider_user, date=self.day.isoformat(), staff=str(self.sam.pk))
        self.assertNotContains(response, "Grace Hopper")

    def test_day_view_has_a_column_per_provider(self):
        response = self.get(self.owner, view="day", date=self.day.isoformat())
        self.assertContains(response, ">Maya</span>")
        self.assertContains(response, ">Sam</span>")

    def test_month_view(self):
        response = self.get(self.owner, view="month", date=self.day.isoformat())
        self.assertContains(response, "cal-chip")
        self.assertContains(response, "Ada Lovelace")

    def test_customers_and_strangers_are_refused(self):
        customer = f.MembershipFactory(organization=self.org, role=OrganizationRole.CUSTOMER).user
        self.assertEqual(self.get(customer).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get("/app/calendar/").status_code, 302)

    def test_query_count_does_not_grow_with_appointments(self):
        self.client.force_login(self.owner)
        url = f"/app/calendar/?view=week&date={self.day.isoformat()}"
        self.client.get(url)
        with CaptureQueriesContext(connection) as few:
            self.client.get(url)
        for hour in range(11, 18):
            self.appointment(self.maya, hour=hour, customer_name=f"Extra {hour}")
            self.appointment(self.sam, hour=hour, customer_name=f"More {hour}")
        with CaptureQueriesContext(connection) as many:
            self.client.get(url)
        self.assertEqual(len(many), len(few))


class PanelAndActionTests(CalendarFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.mine = self.appointment(self.maya, internal_notes="Prefers a quiet room")
        self.theirs = self.appointment(self.sam)

    def panel(self, user, booking, **headers):
        self.client.force_login(user)
        return self.client.get(f"/app/appointments/{booking.pk}/", **headers)

    def act(self, user, booking, action, htmx=True, **data):
        self.client.force_login(user)
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.client.post(
            f"/app/appointments/{booking.pk}/action/", {"action": action, **data}, **headers
        )

    def test_panel_partial_for_htmx(self):
        response = self.panel(self.owner, self.mine, HTTP_HX_REQUEST="true")
        self.assertContains(response, "Check in")
        self.assertNotContains(response, "<html")

    def test_scoping(self):
        self.assertEqual(self.panel(self.provider_user, self.theirs).status_code, 404)
        self.assertEqual(self.panel(self.owner, f.BookingFactory()).status_code, 404)

    def test_internal_notes_need_the_private_notes_capability(self):
        self.assertContains(self.panel(self.owner, self.mine), "Prefers a quiet room")
        self.assertNotContains(self.panel(self.receptionist, self.mine), "Prefers a quiet room")

    def test_check_in_too_early_is_explained(self):
        response = self.act(self.receptionist, self.mine, "check_in")
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "an hour before", status_code=422)

    def test_check_in_and_out(self):
        f.make_current(self.mine)
        response = self.act(self.receptionist, self.mine, "check_in")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Trigger"], "calendar-refresh")
        self.assertContains(response, "Checked In")
        self.act(self.receptionist, self.mine, "check_out")
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.status, Status.COMPLETED)
        row = self.mine.status_history.order_by("created_at").last()
        self.assertEqual(row.source, Booking.Source.RECEPTION)

    def test_cancel_with_reason_without_javascript(self):
        response = self.act(self.provider_user, self.mine, "cancel", htmx=False, reason="Sick")
        self.assertRedirects(response, f"/app/appointments/{self.mine.pk}/")
        self.mine.refresh_from_db()
        self.assertEqual((self.mine.status, self.mine.cancellation_reason), ("cancelled", "Sick"))
        self.assertEqual(
            self.mine.status_history.order_by("created_at").last().source, Booking.Source.STAFF
        )

    def test_providers_cannot_act_on_others(self):
        self.assertEqual(self.act(self.provider_user, self.theirs, "cancel").status_code, 404)

    def test_actions_need_appointments_manage(self):
        viewer = f.MembershipFactory(
            organization=self.org,
            role=OrganizationRole.RECEPTIONIST,
            revoked_permissions=["appointments.manage"],
        ).user
        self.assertEqual(self.panel(viewer, self.mine).status_code, 200)
        self.assertNotContains(self.panel(viewer, self.mine), "Check in")
        self.assertEqual(self.act(viewer, self.mine, "cancel").status_code, 403)

    def test_unknown_action(self):
        self.assertEqual(self.act(self.owner, self.mine, "teleport").status_code, 422)


class EventsFeedTests(CalendarFixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.mine = self.appointment(self.maya, internal_notes="Secret")
        self.theirs = self.appointment(self.sam)
        f.BookingFactory()  # another organization

    def feed(self, user, **params):
        self.client.force_login(user)
        defaults = {
            "start": self.day.isoformat(),
            "end": (self.day + timedelta(days=1)).isoformat(),
        }
        return self.client.get("/app/calendar/events.json", {**defaults, **params})

    def test_feed(self):
        data = self.feed(self.owner).json()
        self.assertEqual(
            {event["id"] for event in data["events"]}, {str(self.mine.pk), str(self.theirs.pk)}
        )
        self.assertNotIn("Secret", str(data))
        self.assertEqual(
            set(data["events"][0]),
            {"id", "title", "start", "end", "status", "staff", "service", "location", "url"},
        )

    def test_feed_is_scoped_and_bounded(self):
        ids = {event["id"] for event in self.feed(self.provider_user).json()["events"]}
        self.assertEqual(ids, {str(self.mine.pk)})
        self.assertEqual(
            self.feed(self.owner, end=(self.day + timedelta(days=90)).isoformat()).status_code, 400
        )
        self.assertEqual(self.feed(self.owner, start="soon").status_code, 400)
