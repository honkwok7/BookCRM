"""M1.4 audit framework tests."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework import viewsets
from rest_framework.test import APITestCase

from api.urls import router
from bookings.services import (
    cancel_booking,
    change_booking_status,
    create_booking,
    reschedule_booking,
)
from core.api import AuditedModelViewSetMixin
from core.audit import (
    REDACTED,
    AuditAction,
    client_ip,
    diff_snapshots,
    record_audit,
    scrub,
)
from core.models import AuditLog, AuditLogImmutableError
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.tenancy import suspend_organization
from services.models import Service
from staff.models import StaffProfile
from tests.factories import future, make_bookable

User = get_user_model()


class ScrubAndDiffTests(SimpleTestCase):
    def test_scrub_redacts_sensitive_keys_at_any_depth(self):
        data = {
            "password": "hunter2",
            "nested": {"api_key": "k", "Card_Number": "4111", "ok": 1},
            "items": [{"refresh_token": "t", "name": "x"}],
            "amount": Decimal("9.50"),
        }
        self.assertEqual(
            scrub(data),
            {
                "password": REDACTED,
                "nested": {"api_key": REDACTED, "Card_Number": REDACTED, "ok": 1},
                "items": [{"refresh_token": REDACTED, "name": "x"}],
                "amount": "9.50",
            },
        )

    def test_diff_reports_changes_and_redacts_personal_fields(self):
        before = {"email": "a@x.test", "price": Decimal("10"), "staff_id": 1, "same": 1}
        after = {"email": "b@x.test", "price": Decimal("20"), "staff_id": 2, "same": 1}
        self.assertEqual(
            diff_snapshots(before, after, redact_fields=("email",)),
            {"email": "changed", "price": ["10", "20"], "staff_id": [1, 2]},
        )

    def test_diff_never_records_secret_values(self):
        self.assertEqual(
            diff_snapshots({"password": "old"}, {"password": "new"}), {"password": "changed"}
        )


class ClientIpTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def request(self):
        return self.factory.get(
            "/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.9"
        )

    def test_forwarded_header_ignored_by_default(self):
        self.assertEqual(client_ip(self.request()), "10.0.0.2")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_uses_address_appended_by_trusted_proxy(self):
        # The left-most entry is client-controlled; only the proxy-appended one is trusted.
        self.assertEqual(client_ip(self.request()), "203.0.113.9")


class AuditLogModelTests(TestCase):
    def test_entries_are_append_only(self):
        log = record_audit(AuditAction.SYSTEM_TEST)
        log.action = "tampered"
        with self.assertRaises(AuditLogImmutableError):
            log.save()
        with self.assertRaises(AuditLogImmutableError):
            log.delete()

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            record_audit("made.up.action")

    def test_actor_type_defaults(self):
        user = User.objects.create_user(email="u@x.test", password="Password12345!")
        self.assertEqual(record_audit(AuditAction.SYSTEM_TEST).actor_type, "system")
        self.assertEqual(record_audit(AuditAction.SYSTEM_TEST, actor=user).actor_type, "user")


class ViewsetCoverageTests(SimpleTestCase):
    """Every mutable tenant viewset in the API must audit its writes."""

    NOT_GENERIC_CRUD = {"booking"}  # bookings mutate only through audited service functions

    def test_mutable_viewsets_are_audited(self):
        for _prefix, viewset, basename in router.registry:
            if basename in self.NOT_GENERIC_CRUD or not issubclass(viewset, viewsets.ModelViewSet):
                continue
            with self.subTest(viewset=viewset.__name__):
                self.assertTrue(issubclass(viewset, AuditedModelViewSetMixin))
                # Writes may instead be audited by the service the serializer calls (declared
                # in ``audited_by_service`` and tested with that service).
                audited = set(viewset.audit_actions) | set(viewset.audited_by_service)
                self.assertEqual(audited, {"create", "update", "delete"})


class AuditedEndpointTests(APITestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="A", slug="org-a")
        self.other = Organization.objects.create(name="B", slug="org-b")
        self.owner = self.member("owner@a.test", OrganizationRole.OWNER)
        self.manager = self.member("manager@a.test", OrganizationRole.MANAGER)
        self.client.force_authenticate(self.owner)

    def member(self, email, role, organization=None):
        user = User.objects.create_user(email=email, password="Password12345!")
        OrganizationMembership.objects.create(
            organization=organization or self.org, user=user, role=role
        )
        return user

    def last(self, action):
        return AuditLog.objects.filter(action=action.value).latest("created_at")

    def test_service_create_update_delete_audited_with_diff(self):
        response = self.client.post(
            "/api/v1/services/",
            {"name": "Massage", "slug": "massage", "price": "10.00", "duration_minutes": 60},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        service_id = response.json()["id"]
        created = self.last(AuditAction.SERVICE_CREATED)
        self.assertEqual(
            (created.organization, created.user, created.object_identifier),
            (self.org, self.owner, service_id),
        )

        self.client.patch(f"/api/v1/services/{service_id}/", {"price": "20.00"}, format="json")
        updated = self.last(AuditAction.SERVICE_UPDATED)
        self.assertEqual(updated.metadata["changes"], {"price": ["10.00", "20.00"]})

        self.client.delete(f"/api/v1/services/{service_id}/")
        self.assertEqual(self.last(AuditAction.SERVICE_DELETED).object_identifier, service_id)

    def test_no_op_update_writes_nothing(self):
        service = Service.objects.create(
            organization=self.org, name="S", slug="s", price=10, duration_minutes=30
        )
        self.client.patch(f"/api/v1/services/{service.id}/", {"price": "10.00"}, format="json")
        self.assertFalse(AuditLog.objects.filter(action="service.updated").exists())

    def test_customer_changes_do_not_store_personal_values(self):
        response = self.client.post(
            "/api/v1/customers/", {"name": "Jo", "email": "jo@x.test"}, format="json"
        )
        customer_id = response.json()["id"]
        self.client.patch(
            f"/api/v1/customers/{customer_id}/", {"email": "new@x.test"}, format="json"
        )
        log = self.last(AuditAction.CUSTOMER_UPDATED)
        self.assertEqual(log.metadata["changes"], {"email": "changed"})
        self.assertNotIn("new@x.test", str(log.metadata))

    def test_failed_write_leaves_no_audit_row(self):
        self.client.post("/api/v1/services/", {"name": "No price"}, format="json")
        self.assertFalse(AuditLog.objects.filter(action="service.created").exists())

    def test_organization_update_audited(self):
        self.client.patch("/api/organizations/current/", {"phone": "555-0100"}, format="json")
        log = self.last(AuditAction.ORGANIZATION_UPDATED)
        self.assertEqual(log.metadata["changes"], {"phone": ["", "555-0100"]})

    def test_invitation_audited_without_email_in_metadata(self):
        self.client.post(
            "/api/organizations/invitations/",
            {"email": "new@a.test", "role": "receptionist"},
            format="json",
        )
        log = self.last(AuditAction.INVITATION_CREATED)
        self.assertEqual(log.metadata, {"role": "receptionist"})

    def test_password_reset_audited_without_secrets(self):
        user = User.objects.create_user(email="r@x.test", password="Password12345!")
        self.client.force_authenticate(None)
        self.client.post(
            "/api/reset-password/",
            {
                "uid": user.pk,
                "token": default_token_generator.make_token(user),
                "new_password": "BrandNewPass123!",
            },
            format="json",
        )
        log = self.last(AuditAction.ACCOUNT_PASSWORD_RESET)
        self.assertEqual(log.user, user)
        self.assertNotIn("BrandNewPass123!", str(log.metadata))


class BookingServiceAuditTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="A", slug="org-a")
        self.actor = User.objects.create_user(email="desk@a.test", password="Password12345!")
        staff = StaffProfile.objects.create(
            user=User.objects.create_user(email="p@a.test", password="Password12345!"),
            organization=self.org,
        )
        service = Service.objects.create(
            organization=self.org, name="S", slug="s", price=10, duration_minutes=30
        )
        make_bookable(staff, service)
        self.booking = create_booking(
            organization=self.org,
            service=service,
            staff_profile=staff,
            customer_name="C",
            customer_email="c@x.test",
            customer_phone="",
            start_datetime=future(2),
            actor=self.actor,
        )

    def actions(self):
        return list(AuditLog.objects.order_by("created_at").values_list("action", flat=True))

    def test_lifecycle_is_audited(self):
        moved = reschedule_booking(
            booking=self.booking,
            new_start=self.booking.start_datetime + timedelta(hours=2),
            actor=self.actor,
        )
        change_booking_status(booking=moved, new_status="checked_in", actor=self.actor)
        cancel = create_booking(
            organization=self.org,
            service=self.booking.service,
            staff_profile=self.booking.staff,
            customer_name="D",
            customer_email="d@x.test",
            customer_phone="",
            start_datetime=future(5),
            actor=self.actor,
        )
        cancel_booking(booking=cancel, actor=self.actor, reason="sick")
        self.assertEqual(
            self.actions(),
            [
                "customer.created",  # the setUp booking's new CRM customer
                "booking.created",
                "booking.created",  # new appointment created by the reschedule
                "booking.rescheduled",
                "booking.status_changed",
                "customer.created",  # "D" is a new customer too
                "booking.created",
                "booking.cancelled",
            ],
        )
        self.assertTrue(
            AuditLog.objects.filter(user=self.actor, organization=self.org)
            .exclude(action="booking.created")
            .exists()
        )

    def test_suspension_audited(self):
        suspend_organization(organization=self.org, reason="unpaid", actor=self.actor)
        log = AuditLog.objects.get(action="organization.suspended")
        self.assertEqual(log.metadata, {"reason": "unpaid"})


class AuditLogApiTests(APITestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="A", slug="org-a")
        self.other = Organization.objects.create(name="B", slug="org-b")
        self.owner = User.objects.create_user(email="owner@a.test", password="Password12345!")
        OrganizationMembership.objects.create(
            organization=self.org, user=self.owner, role=OrganizationRole.OWNER
        )
        self.manager = User.objects.create_user(email="m@a.test", password="Password12345!")
        OrganizationMembership.objects.create(
            organization=self.org, user=self.manager, role=OrganizationRole.MANAGER
        )
        record_audit(AuditAction.SYSTEM_TEST, organization=self.org)
        record_audit(AuditAction.ORGANIZATION_UPDATED, organization=self.org)
        record_audit(AuditAction.SYSTEM_TEST, organization=self.other)

    def test_owner_sees_only_own_organization(self):
        self.client.force_authenticate(self.owner)
        rows = self.client.get("/api/v1/audit-logs/").json()["results"]
        self.assertEqual(len(rows), 2)

    def test_filter_by_action(self):
        self.client.force_authenticate(self.owner)
        rows = self.client.get("/api/v1/audit-logs/", {"action": "organization.updated"}).json()
        self.assertEqual(rows["count"], 1)

    def test_manager_without_audit_view_is_denied(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.get("/api/v1/audit-logs/").status_code, 403)

    def test_audit_api_is_read_only(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.post("/api/v1/audit-logs/", {}).status_code, 405)
