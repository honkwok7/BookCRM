"""Fixes from the role/permission review (Codex): API gaps and capability-only checks."""

from datetime import UTC, datetime, time

from django.test import TestCase
from rest_framework.test import APIClient

from bookings.models import Booking
from bookings.services import create_booking
from organizations.models import OrganizationMembership, OrganizationRole
from tests import factories as f
from tests.test_appointment_screens import Fixtures


class PermissionFixTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.api = APIClient()
        self.booking = create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.maya,
            customer=self.customer,
            customer_name=self.customer.name,
            customer_email=self.customer.email,
            start_datetime=datetime.combine(self.day, time(10), tzinfo=UTC),
            notify=False,
        )

    def test_providers_book_only_themselves_through_the_api(self):
        self.api.force_authenticate(self.provider_user)  # Maya, without view_all
        payload = {
            "service": str(self.service.pk),
            "start_datetime": datetime.combine(self.day, time(14), tzinfo=UTC).isoformat(),
            "customer_name": "Walk Smith",
            "customer_email": "walk@x.test",
        }
        refused = self.api.post(
            "/api/v1/bookings/", payload | {"staff": str(self.sam.pk)}, format="json"
        )
        self.assertEqual(refused.status_code, 400)
        self.assertIn("staff", refused.json())
        own = self.api.post(
            "/api/v1/bookings/", payload | {"staff": str(self.maya.pk)}, format="json"
        )
        self.assertEqual(own.status_code, 201, own.content)

    def test_revoking_manage_stops_api_cancel_and_reschedule(self):
        OrganizationMembership.objects.filter(user=self.receptionist).update(
            revoked_permissions=["appointments.manage"]
        )
        self.api.force_authenticate(self.receptionist)
        url = f"/api/v1/bookings/{self.booking.pk}/"
        self.assertEqual(self.api.post(f"{url}cancel/", {}, format="json").status_code, 403)
        new_start = datetime.combine(self.day, time(15), tzinfo=UTC).isoformat()
        response = self.api.post(f"{url}reschedule/", {"start_datetime": new_start}, format="json")
        self.assertEqual(response.status_code, 403)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)

    def test_the_front_desk_can_still_cancel(self):
        self.api.force_authenticate(self.receptionist)
        response = self.api.post(f"/api/v1/bookings/{self.booking.pk}/cancel/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

    def test_own_customers_follow_a_capability_not_the_role(self):
        self.client.force_login(self.provider_user)
        self.assertEqual(self.client.get(f"/app/customers/{self.customer.pk}/").status_code, 200)
        OrganizationMembership.objects.filter(user=self.provider_user).update(
            revoked_permissions=["customers.view_own"]
        )
        self.assertEqual(self.client.get("/app/customers/").status_code, 403)
        # And a receptionist without customers.view, granted it, sees only their own.
        front = f.MembershipFactory(
            organization=self.org,
            role=OrganizationRole.RECEPTIONIST,
            revoked_permissions=["customers.view", "customers.manage"],
            granted_permissions=["customers.view_own"],
        ).user
        self.client.force_login(front)
        self.assertEqual(self.client.get("/app/customers/").status_code, 200)
        self.assertEqual(self.client.get(f"/app/customers/{self.customer.pk}/").status_code, 404)
