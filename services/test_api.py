"""M3.3: the services API writes through services.services."""

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import AuditLog
from locations.models import Location
from locations.services import create_location
from organizations.models import OrganizationRole
from services.models import Service
from tests import factories as f


class ServiceApiTests(TestCase):
    def setUp(self):
        self.org = f.OrganizationFactory(currency="EUR")
        self.main = Location.objects.get(organization=self.org)
        self.downtown = create_location(organization=self.org, name="Downtown")
        self.client = APIClient()
        self.client.force_authenticate(
            f.MembershipFactory(organization=self.org, role=OrganizationRole.MANAGER).user
        )

    def test_create_with_locations_and_policy(self):
        response = self.client.post(
            "/api/v1/services/",
            {
                "name": "Facial",
                "duration_minutes": 45,
                "price": "90.00",
                "locations": [str(self.downtown.pk)],
                "cancellation_policy": "24 hours' notice",
                "tax_rate": "13.00",
                "slug": "hacked",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(
            (body["slug"], body["currency"], body["locations"], body["tax_rate"]),
            ("facial", "EUR", [str(self.downtown.pk)], "13.00"),
        )
        self.assertEqual(AuditLog.objects.filter(action="service.created").count(), 1)

    def test_location_filter(self):
        f.ServiceFactory(organization=self.org, name="Everywhere")
        limited = f.ServiceFactory(organization=self.org, name="Downtown only")
        limited.locations.set([self.downtown])
        names = lambda location: sorted(  # noqa: E731
            row["name"]
            for row in self.client.get("/api/v1/services/", {"location": str(location.pk)}).json()[
                "results"
            ]
        )
        self.assertEqual(names(self.main), ["Everywhere"])
        self.assertEqual(names(self.downtown), ["Downtown only", "Everywhere"])

    def test_other_tenant_location_rejected(self):
        service = f.ServiceFactory(organization=self.org)
        foreign = Location.objects.get(organization=f.OrganizationFactory())
        response = self.client.patch(
            f"/api/v1/services/{service.pk}/", {"locations": [str(foreign.pk)]}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("locations", response.json())

    def test_delete_booked_service_is_a_conflict(self):
        booking = f.BookingFactory(organization=self.org)
        response = self.client.delete(f"/api/v1/services/{booking.service.pk}/")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "in_use"))
        self.assertTrue(Service.objects.filter(pk=booking.service.pk).exists())

    def test_categories(self):
        response = self.client.post(
            "/api/v1/service-categories/",
            {"name": "Body", "color": "#EC4899", "sort_order": 3},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((response.json()["slug"], response.json()["color"]), ("body", "#ec4899"))
        duplicate = self.client.post("/api/v1/service-categories/", {"name": "body"}, format="json")
        self.assertEqual(duplicate.status_code, 409)

    def test_receptionist_reads_only(self):
        self.client.force_authenticate(
            f.MembershipFactory(organization=self.org, role=OrganizationRole.RECEPTIONIST).user
        )
        self.assertEqual(self.client.get("/api/v1/services/").status_code, 200)
        response = self.client.post(
            "/api/v1/services/", {"name": "X", "duration_minutes": 30, "price": "10"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
