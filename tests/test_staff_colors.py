"""Providers' calendar colours (staff/colors.py): chosen on the staff profile, used everywhere."""

from datetime import UTC, datetime, time

from django.test import TestCase

from bookings.services import create_booking
from organizations.models import OrganizationRole
from staff.colors import COLOR_INDEX, provider_colors
from tests import factories as f

HTMX = {"HTTP_HX_REQUEST": "true"}


class ProviderColorTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory(timezone="UTC")
        self.first = f.StaffProfileFactory(organization=self.org, display_name="First")
        self.second = f.StaffProfileFactory(organization=self.org, display_name="Second")

    def test_automatic_colours_follow_the_order_staff_were_added(self):
        colors = provider_colors(self.org)
        self.assertEqual((colors[self.first.pk], colors[self.second.pk]), (0, 1))

    def test_a_chosen_colour_is_used_and_skipped_by_automatic_ones(self):
        self.second.appointment_color = "sky"
        self.second.save()
        colors = provider_colors(self.org)
        self.assertEqual(colors[self.second.pk], COLOR_INDEX["sky"])
        self.assertEqual(colors[self.first.pk], 1)  # sky is taken

    def test_other_organizations_are_left_out(self):
        f.StaffProfileFactory(appointment_color="rose")
        self.assertEqual(set(provider_colors(self.org)), {self.first.pk, self.second.pk})


class ColorPickerTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory(timezone="UTC")
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.service = f.ServiceFactory(organization=self.org, duration_minutes=60)
        self.maya = f.StaffProfileFactory(organization=self.org, display_name="Maya")
        f.make_bookable(self.maya, self.service, start=time(9), end=time(17))
        self.client.force_login(self.owner)

    def edit(self, **changes):
        url = f"/app/staff/{self.maya.pk}/edit/"
        data = {
            "display_name": "Maya",
            "is_active": "on",
            "is_accepting_bookings": "on",
            "online_booking_visible": "on",
            **changes,
        }
        return self.client.post(url, data, **HTMX)

    def test_the_edit_form_offers_the_swatches(self):
        response = self.client.get(f"/app/staff/{self.maya.pk}/edit/", **HTMX)
        self.assertContains(response, "Calendar colour")
        self.assertContains(response, 'value="violet"')
        self.assertContains(response, "cal-swatch-11")

    def test_choosing_a_colour_colours_the_calendar(self):
        self.assertLess(self.edit(appointment_color="violet").status_code, 300)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.appointment_color, "violet")
        day = f.future(2).date()
        create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.maya,
            customer_name="Ada Lovelace",
            customer_email="ada@x.test",
            start_datetime=datetime.combine(day, time(10), tzinfo=UTC),
            notify=False,
        )
        for view in ("day", "week", "month"):
            with self.subTest(view=view):
                response = self.client.get(
                    "/app/calendar/", {"view": view, "date": day.isoformat()}
                )
                self.assertContains(response, f"cal-color-{COLOR_INDEX['violet']} ")
                self.assertContains(response, 'aria-label="Provider colours"')
        profile = self.client.get(f"/app/staff/{self.maya.pk}/")
        self.assertContains(profile, "Violet")

    def test_unknown_colours_are_refused(self):
        response = self.edit(appointment_color="#ff0000")
        self.assertEqual(response.status_code, 422)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.appointment_color, "")
