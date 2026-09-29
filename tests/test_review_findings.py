"""Regression tests for the second codebase review (Codex, 2026-09-28): findings F1-F9, plus
the invitation-email link built from the Host header (found while verifying F5)."""

import importlib
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.db import IntegrityError, migrations, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking, Customer
from bookings.services import create_booking
from core.exceptions import DomainError
from crm.models import CustomerNote
from crm.services import anonymize_customer
from notifications.models import NotificationLog
from notifications.tasks import send_templated_email
from organizations.models import OrganizationInvitation, OrganizationMembership, OrganizationRole
from organizations.services import accept_invitation, create_invitation
from organizations.tasks import send_invitation_email
from tests import factories as f

User = get_user_model()


class F1LegacyCustomerNotesTests(TestCase):
    def test_migration_moves_legacy_notes_into_internal_notes(self):
        customer = f.CustomerFactory()
        Customer.objects.filter(pk=customer.pk).update(notes="Allergic to latex")
        importlib.import_module("crm.migrations.0005_move_legacy_customer_notes").move_notes(
            apps, None
        )
        customer.refresh_from_db()
        self.assertEqual(customer.notes, "")
        note = CustomerNote.objects.get(customer=customer)
        self.assertEqual((note.content, note.visibility), ("Allergic to latex", "internal"))

    def test_api_neither_exposes_nor_accepts_legacy_notes(self):
        organization = f.OrganizationFactory()
        receptionist = f.MembershipFactory(
            organization=organization, role=OrganizationRole.RECEPTIONIST
        ).user
        customer = f.CustomerFactory(organization=organization, alerts="Uses a wheelchair")
        client = APIClient()
        client.force_authenticate(receptionist)
        body = client.get(f"/api/v1/customers/{customer.pk}/").json()
        self.assertNotIn("notes", body)
        self.assertEqual(body["alerts"], "Uses a wheelchair")  # front-desk alert, by design
        client.patch(f"/api/v1/customers/{customer.pk}/", {"notes": "secret"}, format="json")
        customer.refresh_from_db()
        self.assertEqual(customer.notes, "")


class F2AccessTokenRevocationTests(TestCase):
    def test_password_reset_invalidates_existing_access_tokens(self):
        membership = f.MembershipFactory()
        user = membership.user
        client = APIClient()
        login = client.post(
            "/api/login/", {"email": user.email, "password": f.DEFAULT_PASSWORD}, format="json"
        )
        access = login.data["access"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(client.get("/api/profile/").status_code, 200)

        user.refresh_from_db()
        APIClient().post(
            "/api/reset-password/",
            {
                "uid": user.pk,
                "token": default_token_generator.make_token(user),
                "new_password": "Brand-New-Passphrase-7",
            },
            format="json",
        )
        self.assertEqual(client.get("/api/profile/").status_code, 401)


class F3TagMigrationRollbackTests(TestCase):
    def test_reversing_the_backfill_deletes_nothing(self):
        migration = importlib.import_module("crm.migrations.0002_backfill_tags_from_json")
        operation = migration.Migration.operations[0]
        self.assertIs(operation.reverse_code, migrations.RunPython.noop)


class F4EmailCaseTests(TestCase):
    def test_emails_are_stored_lower_case_and_unique_ignoring_case(self):
        user = User.objects.create_user(email="Sam@Example.TEST", password="x-Long-Pass-99")
        self.assertEqual(user.email, "sam@example.test")
        # bulk_create bypasses save() (and its lower-casing): the database itself must refuse.
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(email="SAM@example.test", username="dup")])

    def test_login_ignores_email_case(self):
        User.objects.create_user(email="sam@example.test", password="x-Long-Pass-99")
        response = APIClient().post(
            "/api/login/", {"email": "SAM@Example.test", "password": "x-Long-Pass-99"}
        )
        self.assertEqual(response.status_code, 200)

    def test_registration_race_is_a_validation_error(self):
        payload = {
            "email": "race@example.test",
            "password": "Correct-Horse-Battery-9",
            "accept_terms": True,
            "accept_privacy": True,
        }
        with mock.patch.object(
            User.objects, "create_user", side_effect=IntegrityError("duplicate")
        ):
            response = APIClient().post("/api/register/", payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)


class F5InvitationTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.owner = f.MembershipFactory(organization=self.organization).user

    def invite(self, email, role=OrganizationRole.RECEPTIONIST):
        return f.InvitationFactory(organization=self.organization, email=email, role=role)

    def test_existing_member_is_never_demoted(self):
        invitation = self.invite(self.owner.email)
        with self.assertRaises(ValueError):
            accept_invitation(invitation=invitation, user=self.owner)
        membership = OrganizationMembership.objects.get(user=self.owner)
        self.assertEqual(membership.role, OrganizationRole.OWNER)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.accepted_at)

    def test_former_member_is_reactivated_with_the_invited_role(self):
        former = f.MembershipFactory(
            organization=self.organization, role=OrganizationRole.MANAGER, is_active=False
        )
        membership = accept_invitation(invitation=self.invite(former.user.email), user=former.user)
        self.assertTrue(membership.is_active)
        self.assertEqual(membership.role, OrganizationRole.RECEPTIONIST)

    def test_invitation_is_used_exactly_once(self):
        user = f.UserFactory()
        invitation = self.invite(user.email)
        accept_invitation(invitation=invitation, user=user)
        OrganizationMembership.objects.filter(user=user).update(is_active=False)
        with self.assertRaises(ValueError):
            accept_invitation(invitation=invitation, user=user)  # stale copy, already used


class InvitationEmailTests(TestCase):
    @override_settings(SITE_URL="https://app.example")
    @mock.patch("organizations.services.send_invitation_email.delay")
    def test_email_is_queued_after_commit_with_a_site_url_link(self, delay):
        organization = f.OrganizationFactory()
        owner = f.MembershipFactory(organization=organization).user
        with self.captureOnCommitCallbacks(execute=True):
            invitation = create_invitation(
                organization=organization, inviter=owner, email="new@example.test", role="staff"
            )
        delay.assert_called_once_with(invitation_id=str(invitation.pk))
        send_invitation_email.apply(kwargs=delay.call_args.kwargs).get()
        self.assertIn(
            f"https://app.example/accept-invitation/?token={invitation.token}",
            mail.outbox[0].body,
        )

    def test_used_invitation_is_not_emailed(self):
        invitation = f.InvitationFactory(accepted_at=timezone.now())
        send_invitation_email.apply(kwargs={"invitation_id": str(invitation.pk)}).get()
        self.assertEqual(mail.outbox, [])
        self.assertTrue(OrganizationInvitation.objects.filter(pk=invitation.pk).exists())


class F6BookingTenantConsistencyTests(TestCase):
    def test_service_refuses_other_tenant_service_or_staff(self):
        organization = f.OrganizationFactory()
        own_service = f.ServiceFactory(organization=organization)
        own_staff = f.StaffProfileFactory(organization=organization)
        for service, staff in (
            (f.ServiceFactory(), own_staff),
            (own_service, f.StaffProfileFactory()),
        ):
            with self.subTest(service=service.organization_id, staff=staff.organization_id):
                with self.assertRaises(DomainError):
                    create_booking(
                        organization=organization,
                        service=service,
                        staff_profile=staff,
                        customer_name="Ada",
                        customer_email="ada@example.test",
                        customer_phone="",
                        start_datetime=timezone.now() + timedelta(days=2),
                        notify=False,
                    )
        self.assertFalse(Booking.objects.exists())

    def test_admin_cannot_edit_bookings_or_customers(self):
        request = RequestFactory().get("/admin/")
        request.user = User.objects.create_superuser(email="root@x.test", password="x-Long-9")
        for model in (Booking, Customer):
            model_admin = admin.site._registry[model]
            with self.subTest(model=model.__name__):
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))
                self.assertTrue(model_admin.has_view_permission(request))


class F8NotificationClaimTests(TestCase):
    def setUp(self):
        self.booking = f.BookingFactory()
        self.log = f.NotificationLogFactory(
            organization=self.booking.organization, related_booking=self.booking
        )

    def send(self):
        send_templated_email.apply(
            kwargs={
                "notification_log_id": str(self.log.pk),
                "organization_id": str(self.booking.organization_id),
                "subject": "Booking confirmed",
                "template_base": "booking_confirmation",
            }
        ).get()

    def test_a_redelivered_task_sends_nothing(self):
        self.send()
        self.send()
        self.assertEqual(len(mail.outbox), 1)
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, NotificationLog.Status.SENT)

    def test_a_task_that_finds_the_log_already_claimed_sends_nothing(self):
        NotificationLog.objects.filter(pk=self.log.pk).update(status=NotificationLog.Status.SENDING)
        self.send()
        self.assertEqual(mail.outbox, [])

    def test_a_failed_log_can_be_retried(self):
        NotificationLog.objects.filter(pk=self.log.pk).update(status=NotificationLog.Status.FAILED)
        self.send()
        self.assertEqual(len(mail.outbox), 1)


class F9AnonymizationClearsDeliveryErrorsTests(TestCase):
    def test_failure_reason_is_cleared(self):
        booking = f.BookingFactory()
        f.NotificationLogFactory(
            organization=booking.organization,
            related_booking=booking,
            failure_reason="550 <grace@example.test>: mailbox unavailable",
        )
        anonymize_customer(customer=booking.customer)
        self.assertEqual(
            list(NotificationLog.objects.values_list("failure_reason", flat=True)), [""]
        )
