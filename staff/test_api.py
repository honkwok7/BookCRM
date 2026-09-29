"""M3.2: the staff API (/api/v1/staff/, /api/v1/staff-offerings/, services/{id}/providers/)."""

from django.test import TestCase
from rest_framework.test import APIClient

from locations.models import Location
from locations.services import create_location
from organizations.models import OrganizationRole
from staff.models import StaffServiceOffering
from staff.services import add_offering, create_staff_profile
from tests import factories as f


class StaffApiTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.main = Location.objects.get(organization=self.org)
        self.downtown = create_location(organization=self.org, name="Downtown")
        self.client = APIClient()
        self.manager = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.MANAGER
        ).user
        self.client.force_authenticate(self.manager)

    def member(self, role=OrganizationRole.STAFF):
        return f.MembershipFactory(organization=self.org, role=role).user

    def test_create_with_locations(self):
        user = self.member()
        response = self.client.post(
            "/api/v1/staff/",
            {
                "user": str(user.pk),
                "display_name": "Maya",
                "locations": [str(self.downtown.pk)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(
            (body["public_name"], body["locations"]), ("Maya", [str(self.downtown.pk)])
        )

    def test_create_rejects_non_members_and_other_tenant_locations(self):
        response = self.client.post(
            "/api/v1/staff/", {"user": str(f.UserFactory().pk)}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        foreign = Location.objects.get(organization=f.OrganizationFactory())
        response = self.client.post(
            "/api/v1/staff/",
            {"user": str(self.member().pk), "locations": [str(foreign.pk)]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("locations", response.json())

    def test_customer_member_cannot_become_staff(self):
        response = self.client.post(
            "/api/v1/staff/",
            {"user": str(self.member(OrganizationRole.CUSTOMER).pk)},
            format="json",
        )
        self.assertEqual((response.status_code, response.json()["code"]), (400, "not_a_member"))

    def test_update_locations_and_filter(self):
        staff = create_staff_profile(organization=self.org, user=self.member())
        response = self.client.patch(
            f"/api/v1/staff/{staff.pk}/",
            {"locations": [str(self.main.pk), str(self.downtown.pk)], "job_title": "Lead"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(staff.locations.count(), 2)
        rows = self.client.get("/api/v1/staff/", {"location": str(self.downtown.pk)}).json()
        self.assertEqual([row["id"] for row in rows["results"]], [str(staff.pk)])

    def test_receptionist_reads_but_cannot_write(self):
        self.client.force_authenticate(self.member(OrganizationRole.RECEPTIONIST))
        self.assertEqual(self.client.get("/api/v1/staff/").status_code, 200)
        response = self.client.post(
            "/api/v1/staff/", {"user": str(self.member().pk)}, format="json"
        )
        self.assertEqual(response.status_code, 403)


class OfferingApiTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory()
        self.main = Location.objects.get(organization=self.org)
        self.downtown = create_location(organization=self.org, name="Downtown")
        owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.client = APIClient()
        self.client.force_authenticate(owner)
        user = f.MembershipFactory(organization=self.org, role=OrganizationRole.STAFF).user
        self.staff = create_staff_profile(
            organization=self.org, user=user, locations=[self.main, self.downtown]
        )
        self.service = f.ServiceFactory(organization=self.org, duration_minutes=60, price=100)

    def test_crud(self):
        response = self.client.post(
            "/api/v1/staff-offerings/",
            {
                "staff": str(self.staff.pk),
                "service": str(self.service.pk),
                "location": str(self.downtown.pk),
                "custom_duration_minutes": 75,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual((body["duration_minutes"], body["price"]), (75, "100.00"))
        url = f"/api/v1/staff-offerings/{body['id']}/"

        response = self.client.patch(url, {"custom_price": "80.00"}, format="json")
        self.assertEqual(response.json()["price"], "80.00")
        response = self.client.patch(url, {"location": str(self.main.pk)}, format="json")
        self.assertEqual(response.status_code, 400)

        duplicate = self.client.post(
            "/api/v1/staff-offerings/",
            {
                "staff": str(self.staff.pk),
                "service": str(self.service.pk),
                "location": str(self.downtown.pk),
            },
            format="json",
        )
        self.assertEqual((duplicate.status_code, duplicate.json()["code"]), (409, "duplicate"))
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(StaffServiceOffering.objects.exists())

    def test_location_not_worked_at_is_rejected(self):
        self.staff.locations.set([self.main])
        response = self.client.post(
            "/api/v1/staff-offerings/",
            {
                "staff": str(self.staff.pk),
                "service": str(self.service.pk),
                "location": str(self.downtown.pk),
            },
            format="json",
        )
        self.assertEqual(
            (response.status_code, response.json()["code"]), (400, "location_not_assigned")
        )

    def test_providers_for_a_service(self):
        add_offering(staff=self.staff, service=self.service, location=self.downtown)
        url = f"/api/v1/services/{self.service.pk}/providers/"
        ids = lambda response: [row["id"] for row in response.json()]  # noqa: E731
        self.assertEqual(ids(self.client.get(url)), [str(self.staff.pk)])
        self.assertEqual(
            ids(self.client.get(url, {"location": str(self.downtown.pk)})), [str(self.staff.pk)]
        )
        self.assertEqual(ids(self.client.get(url, {"location": str(self.main.pk)})), [])
        foreign = Location.objects.get(organization=f.OrganizationFactory())
        self.assertEqual(self.client.get(url, {"location": str(foreign.pk)}).status_code, 400)
        self.assertEqual(self.client.get(url, {"location": "nope"}).status_code, 400)

    def test_assigned_staff_members_on_a_service_writes_offerings(self):
        response = self.client.patch(
            f"/api/v1/services/{self.service.pk}/",
            {"assigned_staff_members": [str(self.staff.pk)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        offering = StaffServiceOffering.objects.get()
        self.assertEqual((offering.staff, offering.location), (self.staff, None))
        response = self.client.patch(
            f"/api/v1/services/{self.service.pk}/", {"assigned_staff_members": []}, format="json"
        )
        self.assertFalse(StaffServiceOffering.objects.exists())
        self.assertEqual(response.json()["assigned_staff_members"], [])


class PublicStaffExposureTests(TestCase):
    def test_public_page_shows_display_names_and_hides_team_only_staff(self):
        org = f.OrganizationFactory(slug="glow")
        f.ServiceFactory(organization=org, is_public=True)
        visible_user = f.UserFactory(first_name="Maya", last_name="Chen", email="maya@private.test")
        f.StaffProfileFactory(organization=org, user=visible_user, display_name="Maya C.")
        hidden_user = f.UserFactory(
            first_name="Ines", last_name="Hidden", email="ines@private.test"
        )
        f.StaffProfileFactory(organization=org, user=hidden_user, online_booking_visible=False)

        body = self.client.get("/book/glow/").content.decode()
        self.assertIn("Maya C.", body)
        self.assertNotIn("Chen", body)
        self.assertNotIn("private.test", body)
        self.assertNotIn("Ines", body)
