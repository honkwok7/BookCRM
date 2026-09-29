"""M3.1: the location screens, under the same access rules as the API."""

from datetime import date, time, timedelta

from django.test import TestCase
from django.urls import reverse

from locations.models import Location, LocationClosure
from locations.services import create_closure, set_location_hours
from locations.tests import subscribe
from organizations.models import OrganizationRole
from tests import factories as f

HTMX = {"HX-Request": "true"}


def member(role, organization):
    return f.MembershipFactory(organization=organization, role=role).user


def hours_post(**days):
    """Form data for the hours editor: ``days={0: [("09:00", "17:00")], ...}``."""
    data = {}
    for day, periods in days.items():
        day = int(day)
        data[f"open_{day}"] = "on"
        for index, (opens_at, closes_at) in enumerate(periods):
            suffix = "2" if index else ""
            data[f"opens{suffix}_{day}"] = opens_at
            data[f"closes{suffix}_{day}"] = closes_at
    return data


class LocationWebTestCase(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory(timezone="America/Toronto")
        self.main = Location.objects.get(organization=self.org)
        self.owner = member(OrganizationRole.OWNER, self.org)
        self.receptionist = member(OrganizationRole.RECEPTIONIST, self.org)
        self.outsider = Location.objects.get(organization=f.OrganizationFactory())


class LocationPagesTests(LocationWebTestCase):
    def test_list_and_detail(self):
        set_location_hours(location=self.main, periods=[(d, time(9), time(17)) for d in range(7)])
        create_closure(
            location=self.main, start_date=date.today() + timedelta(days=3), reason="Stocktake"
        )
        self.client.force_login(self.owner)

        page = self.client.get(reverse("app-location-list")).content.decode()
        self.assertIn("Main", page)
        self.assertIn("09:00–17:00", page)
        self.assertIn(reverse("app-location-new"), page)
        self.assertNotIn(str(self.outsider.pk), page)

        page = self.client.get(reverse("app-location-detail", args=[self.main.pk])).content.decode()
        self.assertIn("Default location", page)
        self.assertIn("Stocktake", page)
        self.assertIn(reverse("app-location-hours", args=[self.main.pk]), page)

    def test_read_only_roles_see_no_edit_controls(self):
        for role in (OrganizationRole.RECEPTIONIST, OrganizationRole.STAFF):
            with self.subTest(role=role):
                self.client.force_login(member(role, self.org))
                page = self.client.get(reverse("app-location-list"))
                self.assertEqual(page.status_code, 200)
                self.assertNotIn(reverse("app-location-new"), page.content.decode())
                detail = self.client.get(reverse("app-location-detail", args=[self.main.pk]))
                self.assertNotIn(
                    reverse("app-location-edit", args=[self.main.pk]), detail.content.decode()
                )

    def test_access_rules(self):
        self.client.force_login(self.receptionist)
        for url in (
            reverse("app-location-new"),
            reverse("app-location-edit", args=[self.main.pk]),
            reverse("app-location-hours", args=[self.main.pk]),
            reverse("app-location-closure-new", args=[self.main.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, {"name": "X"}).status_code, 403)
        self.assertEqual(Location.objects.filter(organization=self.org).count(), 1)

        self.client.force_login(member(OrganizationRole.CUSTOMER, self.org))
        self.assertEqual(self.client.get(reverse("app-location-list")).status_code, 403)

    def test_other_organization_location_is_404(self):
        self.client.force_login(self.owner)
        for name in ("app-location-detail", "app-location-edit", "app-location-hours"):
            with self.subTest(page=name):
                url = reverse(name, args=[self.outsider.pk])
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_plan_limit_hides_the_add_button(self):
        subscribe(self.org, maximum_locations=1)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("app-location-list")).content.decode()
        self.assertIn("1 of 1 active location on your plan", page)
        self.assertNotIn(reverse("app-location-new"), page)
        response = self.client.post(
            reverse("app-location-new"), {"name": "Second", "timezone": "UTC"}, headers=HTMX
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Location limit reached", response.content.decode())


class LocationFormTests(LocationWebTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_add_location_with_htmx(self):
        form = self.client.get(reverse("app-location-new"), headers=HTMX)
        self.assertContains(form, 'value="America/Toronto" selected')
        response = self.client.post(
            reverse("app-location-new"),
            {
                "name": "Downtown",
                "timezone": "America/Toronto",
                "city": "Toronto",
                "booking_enabled": "on",
            },
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        location = Location.objects.get(organization=self.org, name="Downtown")
        self.assertEqual(
            response["HX-Redirect"], reverse("app-location-detail", args=[location.pk])
        )
        self.assertTrue(location.booking_enabled)
        self.assertFalse(location.is_default)

    def test_add_location_without_javascript(self):
        response = self.client.post(
            reverse("app-location-new"), {"name": "Annex", "timezone": "UTC"}
        )
        location = Location.objects.get(organization=self.org, name="Annex")
        self.assertRedirects(response, reverse("app-location-detail", args=[location.pk]))

    def test_invalid_form_is_422(self):
        response = self.client.post(
            reverse("app-location-new"), {"name": "", "timezone": "Mars/Base"}, headers=HTMX
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(Location.objects.filter(organization=self.org).count(), 1)

    def test_edit_location(self):
        response = self.client.post(
            reverse("app-location-edit", args=[self.main.pk]),
            {"name": "Head office", "timezone": "America/Toronto", "is_active": "on"},
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        self.main.refresh_from_db()
        self.assertEqual((self.main.name, self.main.booking_enabled), ("Head office", False))

    def test_deactivating_the_default_shows_the_rule(self):
        response = self.client.post(
            reverse("app-location-edit", args=[self.main.pk]),
            {"name": "Main", "timezone": "UTC"},
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("make another location the default", response.content.decode().lower())
        self.main.refresh_from_db()
        self.assertTrue(self.main.is_active)

    def test_make_default(self):
        annex = f.LocationFactory(organization=self.org)
        response = self.client.post(reverse("app-location-make-default", args=[annex.pk]))
        self.assertRedirects(response, reverse("app-location-detail", args=[annex.pk]))
        annex.refresh_from_db()
        self.assertTrue(annex.is_default)


class HoursEditorTests(LocationWebTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.url = reverse("app-location-hours", args=[self.main.pk])

    def test_save_hours(self):
        data = hours_post(
            **{"0": [("09:00", "12:00"), ("13:00", "18:00")], "5": [("10:00", "14:00")]}
        )
        response = self.client.post(self.url, data, headers=HTMX)
        self.assertEqual(response.status_code, 204)
        periods = list(self.main.hours.values_list("weekday", "opens_at", "closes_at"))
        self.assertEqual(
            periods,
            [(0, time(9), time(12)), (0, time(13), time(18)), (5, time(10), time(14))],
        )
        form = self.client.get(self.url, headers=HTMX).content.decode()
        self.assertIn('name="opens2_0" value="13:00"', form)

    def test_invalid_hours_are_explained(self):
        cases = [
            ({"open_1": "on"}, "Enter the opening and closing times for Tuesday."),
            (
                hours_post(**{"1": [("17:00", "09:00")]}),
                "Closing time must be after the opening time.",
            ),
            (
                hours_post(**{"1": [("09:00", "13:00"), ("12:00", "17:00")]}),
                "must start after the first ends",
            ),
        ]
        for data, message in cases:
            with self.subTest(message=message):
                response = self.client.post(self.url, data, headers=HTMX)
                self.assertEqual(response.status_code, 422)
                self.assertIn(message, response.content.decode())
        self.assertFalse(self.main.hours.exists())

    def test_unticking_every_day_clears_the_hours(self):
        set_location_hours(location=self.main, periods=[(0, time(9), time(17))])
        self.assertEqual(self.client.post(self.url, {}, headers=HTMX).status_code, 204)
        self.assertFalse(self.main.hours.exists())


class ClosureScreenTests(LocationWebTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.day = date.today() + timedelta(days=5)

    def test_add_and_remove_a_closure(self):
        url = reverse("app-location-closure-new", args=[self.main.pk])
        response = self.client.post(
            url,
            {"start_date": self.day.isoformat(), "all_day": "on", "reason": "Holiday"},
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 204)
        closure = LocationClosure.objects.get()
        self.assertEqual((closure.location, closure.end_date), (self.main, self.day))

        delete_url = reverse("app-location-closure-delete", args=[self.main.pk, closure.pk])
        self.assertEqual(self.client.post(delete_url, headers=HTMX).status_code, 204)
        self.assertFalse(LocationClosure.objects.exists())

    def test_partial_closure_needs_times(self):
        url = reverse("app-location-closure-new", args=[self.main.pk])
        response = self.client.post(url, {"start_date": self.day.isoformat()}, headers=HTMX)
        self.assertEqual(response.status_code, 422)
        self.assertIn("Enter the times the location is closed", response.content.decode())

    def test_closure_of_another_location_is_404(self):
        closure = f.LocationClosureFactory(
            organization=self.outsider.organization, location=self.outsider
        )
        url = reverse("app-location-closure-delete", args=[self.main.pk, closure.pk])
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertTrue(LocationClosure.objects.filter(pk=closure.pk).exists())
