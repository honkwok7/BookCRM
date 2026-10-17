"""Fixes from the second role/permission review (Codex)."""

from datetime import UTC, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
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


class WeeklyBlockLimitTests(Fixtures, TestCase):
    """Self-approved blocks add up to at most 12 hours a week (Monday to Sunday)."""

    def setUp(self):
        self.make_fixtures()  # organization in UTC
        today = timezone.now().date()
        monday = today + timedelta(days=7 - today.weekday())  # next Monday
        self.monday = datetime.combine(monday, time(8), tzinfo=UTC)

    def block(self, day, hours):
        start = self.monday + timedelta(days=day)
        return block_time(staff=self.maya, start=start, end=start + timedelta(hours=hours))

    def test_blocks_on_different_days_add_up(self):
        self.block(0, 8)  # Monday
        self.block(2, 4)  # Wednesday: 12 hours in all
        with self.assertRaisesMessage(ValidationError, "12 hours a week"):
            self.block(4, 1)  # Friday
        self.assertEqual(TimeOff.objects.filter(staff=self.maya, self_blocked=True).count(), 2)

    def test_daily_blocks_cannot_cover_a_week(self):
        with self.assertRaises(ValidationError):
            for day in range(5):
                self.block(day, 11)  # 11 hours a day, more than 12 hours apart
        self.assertEqual(TimeOff.objects.filter(staff=self.maya).count(), 1)

    def test_each_week_has_its_own_allowance(self):
        self.block(0, 12)
        self.block(7, 12)  # the next Monday

    def test_approved_requests_do_not_count_as_blocks(self):
        entry = request_time_off(
            staff=self.maya,
            start=self.monday,
            end=self.monday + timedelta(days=1),
            reason="Holiday",
        )
        decide_time_off(entry=entry, approve=True, actor=self.receptionist)
        self.block(2, 12)
        self.assertFalse(TimeOff.objects.get(pk=entry.pk).self_blocked)


class ThirdReviewTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.api = APIClient()
        self.manager = f.MembershipFactory(organization=self.org, role=OrganizationRole.MANAGER)
        self.api.force_authenticate(self.manager.user)

    def revoke(self, *codes):
        OrganizationMembership.objects.filter(pk=self.manager.pk).update(
            revoked_permissions=list(codes)
        )

    def test_choosing_who_offers_a_service_needs_staff_manage(self):
        url = f"/api/v1/services/{self.service.pk}/"
        body = {"assigned_staff_members": [str(self.maya.pk)]}
        self.assertEqual(self.api.patch(url, body, format="json").status_code, 200)
        self.revoke("staff.manage")
        cleared = self.api.patch(url, {"assigned_staff_members": []}, format="json")
        self.assertEqual(cleared.status_code, 403)
        self.assertTrue(self.maya.offerings.filter(service=self.service, is_active=True).exists())
        # The rest of the service is still theirs to edit.
        renamed = self.api.patch(url, {"name": "Deep tissue"}, format="json")
        self.assertEqual(renamed.status_code, 200)

    def test_organization_closures_need_locations_manage(self):
        url = "/api/v1/availability/holidays/"
        body = {"date": f.future(30).date().isoformat(), "name": "Staff day"}
        self.assertEqual(self.api.post(url, body, format="json").status_code, 201)
        self.revoke("locations.manage")
        body["date"] = f.future(31).date().isoformat()
        self.assertEqual(self.api.post(url, body, format="json").status_code, 403)
        self.api.force_authenticate(self.receptionist)  # locations.view only
        self.assertEqual(self.api.get(url).status_code, 200)
        self.assertEqual(self.api.post(url, body, format="json").status_code, 403)
