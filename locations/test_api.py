"""M3.1: the locations API (/api/v1/locations/, /api/v1/location-closures/)."""

from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from locations.models import Location, LocationClosure
from locations.tests import subscribe
from organizations.models import OrganizationRole
from tests import factories as f


class LocationApiTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory(timezone="America/Toronto")
        self.main = Location.objects.get(organization=self.org)
        self.client = APIClient()

    def login(self, role):
        user = f.MembershipFactory(organization=self.org, role=role).user
        self.client.force_authenticate(user)
        return user

    def test_team_roles_can_read_but_only_managers_write(self):
        cases = [
            (OrganizationRole.OWNER, 200, 201),
            (OrganizationRole.MANAGER, 200, 201),
            (OrganizationRole.RECEPTIONIST, 200, 403),
            (OrganizationRole.STAFF, 200, 403),
            (OrganizationRole.CUSTOMER, 403, 403),
        ]
        for role, read, write in cases:
            with self.subTest(role=role):
                self.login(role)
                self.assertEqual(self.client.get("/api/v1/locations/").status_code, read)
                response = self.client.post(
                    "/api/v1/locations/", {"name": f"Site {role}"}, format="json"
                )
                self.assertEqual(response.status_code, write)

    def test_create_and_list(self):
        self.login(OrganizationRole.OWNER)
        response = self.client.post(
            "/api/v1/locations/",
            {
                "name": "Downtown",
                "city": "Toronto",
                "is_default": True,  # read-only: ignored
                "slug": "hacked",  # read-only: ignored
                "booking_settings": {"x": 1},  # read-only: ignored
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(
            (body["slug"], body["is_default"], body["timezone"], body["booking_settings"]),
            ("downtown", False, "America/Toronto", {}),
        )
        names = [row["name"] for row in self.client.get("/api/v1/locations/").json()["results"]]
        self.assertEqual(names, ["Main", "Downtown"])  # default first

    def test_invalid_timezone(self):
        self.login(OrganizationRole.OWNER)
        response = self.client.post(
            "/api/v1/locations/", {"name": "X", "timezone": "Nowhere/City"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("timezone", response.json())

    def test_plan_limit_is_a_conflict(self):
        subscribe(self.org, maximum_locations=1)
        self.login(OrganizationRole.OWNER)
        response = self.client.post("/api/v1/locations/", {"name": "Second"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "plan_limit")

    def test_default_location_rules(self):
        self.login(OrganizationRole.OWNER)
        url = f"/api/v1/locations/{self.main.pk}/"
        response = self.client.patch(url, {"is_active": False}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "default_location"))
        self.assertEqual(self.client.delete(url).status_code, 409)

        annex = f.LocationFactory(organization=self.org)
        response = self.client.post(f"/api/v1/locations/{annex.pk}/make-default/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_default"])
        self.assertEqual(self.client.patch(url, {"is_active": False}).status_code, 200)
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_hours(self):
        self.login(OrganizationRole.MANAGER)
        url = f"/api/v1/locations/{self.main.pk}/hours/"
        hours = [
            {"weekday": 0, "opens_at": "09:00", "closes_at": "12:00"},
            {"weekday": 0, "opens_at": "13:00", "closes_at": "17:00"},
            {"weekday": 5, "opens_at": "10:00", "closes_at": "14:00"},
        ]
        response = self.client.put(url, {"hours": hours}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()), 3)
        self.assertEqual(self.client.get(url).json()[0]["opens_at"], "09:00:00")
        detail = self.client.get(f"/api/v1/locations/{self.main.pk}/").json()
        self.assertEqual(len(detail["hours"]), 3)

        overlapping = [
            {"weekday": 1, "opens_at": "09:00", "closes_at": "13:00"},
            {"weekday": 1, "opens_at": "12:00", "closes_at": "17:00"},
        ]
        response = self.client.put(url, {"hours": overlapping}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "overlapping"))
        self.assertEqual(self.main.hours.count(), 3)

        self.login(OrganizationRole.RECEPTIONIST)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.put(url, {"hours": []}, format="json").status_code, 403)


class LocationClosureApiTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.main = Location.objects.get(organization=self.org)
        self.client = APIClient()
        self.client.force_authenticate(f.MembershipFactory(organization=self.org).user)
        self.day = date.today() + timedelta(days=7)

    def test_create_filter_and_delete(self):
        response = self.client.post(
            "/api/v1/location-closures/",
            {
                "location": str(self.main.pk),
                "start_date": self.day.isoformat(),
                "reason": "Holiday",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["end_date"], self.day.isoformat())
        f.LocationClosureFactory(organization=self.org, start_date=date(2020, 1, 1))

        upcoming = self.client.get(
            "/api/v1/location-closures/",
            {"location": str(self.main.pk), "ends_after": date.today().isoformat()},
        ).json()["results"]
        self.assertEqual([row["reason"] for row in upcoming], ["Holiday"])

        closure_id = response.json()["id"]
        self.assertEqual(
            self.client.delete(f"/api/v1/location-closures/{closure_id}/").status_code, 204
        )

    def test_partial_closure_needs_times(self):
        response = self.client.post(
            "/api/v1/location-closures/",
            {"location": str(self.main.pk), "start_date": self.day.isoformat(), "all_day": False},
            format="json",
        )
        self.assertEqual((response.status_code, response.json()["code"]), (400, "times_required"))

    def test_other_organization_location_is_rejected(self):
        outsider = Location.objects.get(organization=f.OrganizationFactory())
        response = self.client.post(
            "/api/v1/location-closures/",
            {"location": str(outsider.pk), "start_date": self.day.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("does not exist", response.json()["location"][0])
        self.assertFalse(LocationClosure.objects.exists())

    def test_closure_cannot_move_to_another_location(self):
        closure = f.LocationClosureFactory(organization=self.org, location=self.main)
        annex = f.LocationFactory(organization=self.org)
        response = self.client.patch(
            f"/api/v1/location-closures/{closure.pk}/", {"location": str(annex.pk)}, format="json"
        )
        self.assertEqual(response.status_code, 400)
