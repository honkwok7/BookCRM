"""M0 regression tests for the critical tenant leaks found in docs/CURRENT_STATE_ANALYSIS.md (P1-P3, P9)."""

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone
from rest_framework.test import APITestCase

from bookings.models import WaitlistEntry
from bookings.services import create_booking
from notifications.models import NotificationLog
from notifications.tasks import send_templated_email
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from services.models import Service
from staff.models import StaffProfile


User = get_user_model()


class TenantFixtureMixin:
    def setUp(self):
        self.org_a = Organization.objects.create(name="Org A", slug="org-a")
        self.org_b = Organization.objects.create(name="Org B", slug="org-b")

        self.manager_a = self._member(self.org_a, "manager@a.test", OrganizationRole.MANAGER)
        self.customer_a = self._member(self.org_a, "customer@a.test", OrganizationRole.CUSTOMER)
        self.manager_b = self._member(self.org_b, "manager@b.test", OrganizationRole.MANAGER)

        staff_user = User.objects.create_user(email="staff@b.test", password="Staff12345!")
        self.staff_b = StaffProfile.objects.create(user=staff_user, organization=self.org_b)
        self.service_a = Service.objects.create(organization=self.org_a, name="A", slug="a", price=10, duration_minutes=30)
        self.service_b = Service.objects.create(organization=self.org_b, name="B", slug="b", price=10, duration_minutes=30)

        WaitlistEntry.objects.create(
            organization=self.org_a, service=self.service_a,
            customer_name="Alice", customer_email="alice@a.test", customer_phone="111",
        )
        WaitlistEntry.objects.create(
            organization=self.org_b, service=self.service_b,
            customer_name="Secret", customer_email="secret@b.test", customer_phone="555",
        )

    @staticmethod
    def _member(organization, email, role):
        user = User.objects.create_user(email=email, password="Member12345!")
        OrganizationMembership.objects.create(organization=organization, user=user, role=role)
        return user


class WaitlistIsolationTests(TenantFixtureMixin, APITestCase):
    def test_anonymous_cannot_list_any_tenant_waitlist(self):
        response = self.client.get("/api/v1/waitlist/", HTTP_X_ORGANIZATION_SLUG="org-b")
        self.assertEqual(response.status_code, 401)

    def test_member_of_other_tenant_cannot_list_waitlist(self):
        self.client.force_authenticate(self.manager_a)
        response = self.client.get("/api/v1/waitlist/", HTTP_X_ORGANIZATION_SLUG="org-b")
        self.assertEqual(response.status_code, 403)

    def test_customer_role_cannot_list_own_org_waitlist(self):
        self.client.force_authenticate(self.customer_a)
        response = self.client.get("/api/v1/waitlist/")
        self.assertEqual(response.status_code, 403)

    def test_manager_sees_only_own_tenant_entries(self):
        self.client.force_authenticate(self.manager_a)
        response = self.client.get("/api/v1/waitlist/")
        self.assertEqual(response.status_code, 200)
        emails = {row["customer_email"] for row in response.json()["results"]}
        self.assertEqual(emails, {"alice@a.test"})


class DashboardIsolationTests(TenantFixtureMixin, APITestCase):
    def test_member_of_other_tenant_cannot_read_dashboard(self):
        self.client.force_authenticate(self.manager_a)
        response = self.client.get("/api/dashboard/summary/", HTTP_X_ORGANIZATION_SLUG="org-b")
        self.assertEqual(response.status_code, 403)

    def test_customer_role_cannot_read_dashboard(self):
        self.client.force_authenticate(self.customer_a)
        response = self.client.get("/api/dashboard/summary/")
        self.assertEqual(response.status_code, 403)

    def test_manager_reads_own_dashboard(self):
        self.client.force_authenticate(self.manager_a)
        response = self.client.get("/api/dashboard/summary/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["active_services"], 1)


class OrganizationEditTests(TenantFixtureMixin, APITestCase):
    def test_customer_role_cannot_edit_organization(self):
        self.client.force_authenticate(self.customer_a)
        response = self.client.patch("/api/organizations/current/", {"name": "pwned"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.org_a.refresh_from_db()
        self.assertEqual(self.org_a.name, "Org A")

    def test_customer_role_can_still_read_organization(self):
        self.client.force_authenticate(self.customer_a)
        response = self.client.get("/api/organizations/current/")
        self.assertEqual(response.status_code, 200)

    def test_manager_cannot_suspend_or_deactivate_organization(self):
        self.client.force_authenticate(self.manager_a)
        response = self.client.patch(
            "/api/organizations/current/",
            {"name": "Org A Renamed", "is_suspended": True, "is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.org_a.refresh_from_db()
        self.assertEqual(self.org_a.name, "Org A Renamed")
        self.assertFalse(self.org_a.is_suspended)
        self.assertTrue(self.org_a.is_active)

    def test_manager_cannot_edit_other_tenant(self):
        self.client.force_authenticate(self.manager_a)
        response = self.client.patch(
            "/api/organizations/current/", {"name": "pwned"}, format="json", HTTP_X_ORGANIZATION_SLUG="org-b"
        )
        self.assertEqual(response.status_code, 403)
        self.org_b.refresh_from_db()
        self.assertEqual(self.org_b.name, "Org B")


class BookingNotificationDispatchTests(TenantFixtureMixin, APITestCase):
    def _book(self):
        return create_booking(
            organization=self.org_b,
            service=self.service_b,
            staff_profile=self.staff_b,
            customer_name="Carol",
            customer_email="carol@b.test",
            customer_phone="",
            start_datetime=timezone.now() + timedelta(days=1),
        )

    @mock.patch("notifications.services.send_templated_email.delay")
    def test_enqueued_only_after_commit_with_json_safe_ids(self, delay):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            booking = self._book()
        delay.assert_not_called()

        for callback in callbacks:
            callback()
        delay.assert_called_once()
        kwargs = delay.call_args.kwargs
        json.dumps(kwargs)  # must be serializable by Celery's JSON serializer
        self.assertEqual(kwargs["organization_id"], str(self.org_b.id))
        log = NotificationLog.objects.get(id=kwargs["notification_log_id"])
        self.assertEqual(log.related_booking_id, booking.id)

    @mock.patch("notifications.services.send_templated_email.delay", side_effect=ConnectionError("broker down"))
    def test_broker_failure_leaves_log_pending_and_sends_nothing(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            booking = self._book()
        log = NotificationLog.objects.get(related_booking=booking)
        self.assertEqual(log.status, NotificationLog.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)

    @mock.patch("notifications.services.send_templated_email.delay")
    def test_task_sends_email_and_marks_log_sent(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            booking = self._book()
        send_templated_email.apply(kwargs=delay.call_args.kwargs).get()

        log = NotificationLog.objects.get(related_booking=booking)
        self.assertEqual(log.status, NotificationLog.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(booking.reference, mail.outbox[0].body)

    @mock.patch("notifications.services.send_templated_email.delay")
    def test_task_refuses_log_from_another_tenant(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            self._book()
        kwargs = {**delay.call_args.kwargs, "organization_id": str(self.org_a.id)}
        with self.assertRaises(NotificationLog.DoesNotExist):
            send_templated_email.apply(kwargs=kwargs).get()
        self.assertEqual(len(mail.outbox), 0)
