"""Fixes from the second role/permission review (Codex)."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from organizations.models import OrganizationMembership, OrganizationRole
from scheduling.models import TimeOff
from scheduling.services import block_time, decide_time_off, request_time_off
from tests import factories as f
from tests.test_appointment_screens import Fixtures


class SecondReviewTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.api = APIClient()

    def test_my_customers_closes_without_the_capability(self):
        f.BookingFactory(organization=self.org, customer=self.customer, staff=self.maya)
        self.client.force_login(self.provider_user)
        self.assertContains(self.client.get("/staff/customers/"), "Ada")
        OrganizationMembership.objects.filter(user=self.provider_user).update(
            revoked_permissions=["customers.view_own"]
        )
        self.assertEqual(self.client.get("/staff/customers/").status_code, 403)

    def test_who_offers_a_service_needs_staff_view(self):
        url = f"/api/v1/services/{self.service.pk}/providers/"
        self.api.force_authenticate(self.receptionist)
        self.assertEqual(self.api.get(url).status_code, 200)
        OrganizationMembership.objects.filter(user=self.receptionist).update(
            revoked_permissions=["staff.view"]
        )
        self.assertEqual(self.api.get(url).status_code, 403)

    def test_reminders_need_communications_manage(self):
        owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.api.force_authenticate(owner)
        url = "/api/organizations/current/"
        ok = self.api.patch(url, {"reminder_hours_before": 48}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        OrganizationMembership.objects.filter(user=owner).update(
            revoked_permissions=["communications.manage"]
        )
        refused = self.api.patch(url, {"reminder_hours_before": 12}, format="json")
        self.assertEqual(refused.status_code, 403)
        self.org.refresh_from_db()
        self.assertEqual(self.org.reminder_hours_before, 48)
        # The rest of the profile is still theirs to edit.
        self.assertEqual(
            self.api.patch(url, {"booking_instructions": "Hi"}, format="json").status_code, 200
        )


class BlockChainTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.start = f.future(3, hour=8)

    def test_short_blocks_cannot_be_chained_into_a_long_absence(self):
        block_time(staff=self.maya, start=self.start, end=self.start + timedelta(hours=8))
        with self.assertRaisesMessage(ValidationError, "Request time off instead"):
            block_time(
                staff=self.maya,
                start=self.start + timedelta(hours=8),
                end=self.start + timedelta(hours=13),
            )
        # Up to the limit in total is fine.
        block_time(
            staff=self.maya,
            start=self.start + timedelta(hours=9),
            end=self.start + timedelta(hours=12),
        )
        self.assertEqual(TimeOff.objects.filter(staff=self.maya, self_blocked=True).count(), 2)

    def test_approved_requests_do_not_count_as_blocks(self):
        entry = request_time_off(
            staff=self.maya,
            start=self.start - timedelta(days=1),
            end=self.start,
            reason="Holiday",
        )
        decide_time_off(entry=entry, approve=True, actor=self.receptionist)
        block_time(staff=self.maya, start=self.start, end=self.start + timedelta(hours=12))
        self.assertFalse(TimeOff.objects.get(pk=entry.pk).self_blocked)

    def test_blocks_far_apart_are_separate(self):
        block_time(staff=self.maya, start=self.start, end=self.start + timedelta(hours=12))
        later = self.start + timedelta(days=2)
        block_time(staff=self.maya, start=later, end=later + timedelta(hours=12))
