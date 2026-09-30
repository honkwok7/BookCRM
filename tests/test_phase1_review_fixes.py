"""Fixes from the Phase 1 re-review: re-invited members, suspended organizations and
invitations, suspension audit atomicity, organization time zones, the Django admin, and
``seed_demo`` outside development."""

from importlib import import_module
from io import StringIO
from unittest.mock import patch

from django.apps import apps
from django.contrib import admin
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.test import APIClient

from core.audit import AuditLog
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from organizations.services import accept_invitation, create_invitation
from organizations.tasks import send_invitation_email
from organizations.tenancy import reactivate_organization, suspend_organization
from tests import factories as f


class Fixtures:
    def make_fixtures(self):
        self.org = f.OrganizationFactory(slug="glow", timezone="UTC")
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user

    def invite(self, email, role=OrganizationRole.STAFF):
        with self.captureOnCommitCallbacks(execute=False):
            return create_invitation(
                organization=self.org, inviter=self.owner, email=email, role=role
            )


class ReinvitedMemberTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_old_capability_overrides_are_not_inherited(self):
        membership = f.MembershipFactory(
            organization=self.org,
            role=OrganizationRole.MANAGER,
            granted_permissions=["audit.view", "billing.manage"],
            revoked_permissions=["customers.manage"],
        )
        membership.is_active = False
        membership.save()
        invitation = self.invite(membership.user.email)
        joined = accept_invitation(invitation=invitation, user=membership.user)
        self.assertEqual(joined.pk, membership.pk)
        self.assertEqual(joined.role, OrganizationRole.STAFF)
        self.assertEqual(joined.granted_permissions, [])
        self.assertEqual(joined.revoked_permissions, [])
        self.assertNotIn("audit.view", {str(code) for code in joined.capabilities})


class SuspendedOrganizationInvitationTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.user = f.UserFactory(email="new@x.test")
        self.invitation = self.invite("new@x.test")
        suspend_organization(organization=self.org, reason="unpaid")
        self.invitation.refresh_from_db()

    def test_accepting_is_refused_and_the_invitation_stays_unused(self):
        with self.assertRaises(ValueError):
            accept_invitation(invitation=self.invitation, user=self.user)
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.accepted_at)
        self.assertFalse(OrganizationMembership.objects.filter(user=self.user).exists())

    def test_inactive_organization_too(self):
        reactivate_organization(organization=self.org)
        Organization.objects.filter(pk=self.org.pk).update(is_active=False)
        self.invitation.refresh_from_db()
        with self.assertRaises(ValueError):
            accept_invitation(invitation=self.invitation, user=self.user)

    def test_the_email_is_not_sent(self):
        send_invitation_email.run(invitation_id=str(self.invitation.pk))
        self.assertEqual(len(mail.outbox), 0)

    def test_the_link_shows_the_invitation_as_invalid(self):
        self.client.force_login(self.user)
        response = self.client.get(f"/accept-invitation/?token={self.invitation.token}")
        self.assertEqual(response.status_code, 400)
        self.client.post(f"/accept-invitation/?token={self.invitation.token}")
        self.assertFalse(OrganizationMembership.objects.filter(user=self.user).exists())

    def test_after_reactivation_it_works_again(self):
        reactivate_organization(organization=self.org)
        self.invitation.refresh_from_db()
        membership = accept_invitation(invitation=self.invitation, user=self.user)
        self.assertTrue(membership.is_active)


class SuspensionAuditAtomicityTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_suspension_is_undone_when_the_audit_entry_fails(self):
        with patch("core.audit.record_audit", side_effect=RuntimeError("audit down")):
            with self.assertRaises(RuntimeError):
                suspend_organization(organization=self.org, reason="unpaid")
        self.assertFalse(Organization.objects.get(pk=self.org.pk).is_suspended)

    def test_reactivation_is_undone_when_the_audit_entry_fails(self):
        suspend_organization(organization=self.org, reason="unpaid")
        with patch("core.audit.record_audit", side_effect=RuntimeError("audit down")):
            with self.assertRaises(RuntimeError):
                reactivate_organization(organization=self.org)
        self.assertTrue(Organization.objects.get(pk=self.org.pk).is_suspended)


class OrganizationTimezoneTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_unknown_zone_is_refused_by_the_api(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        response = client.patch(
            "/api/organizations/current/",
            {"timezone": "Mars/Olympus"},
            format="json",
            HTTP_X_ORGANIZATION_SLUG="glow",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("timezone", response.json())
        self.assertEqual(Organization.objects.get(pk=self.org.pk).timezone, "UTC")
        ok = client.patch(
            "/api/organizations/current/",
            {"timezone": "America/Toronto"},
            format="json",
            HTTP_X_ORGANIZATION_SLUG="glow",
        )
        self.assertEqual(ok.status_code, 200, ok.content)

    def test_migration_resets_unknown_zones(self):
        Organization.objects.filter(pk=self.org.pk).update(timezone="Mars/Olympus")
        migration = import_module("organizations.migrations.0004_organization_timezone_validator")
        migration.reset_unknown_zones(apps, None)
        self.assertEqual(Organization.objects.get(pk=self.org.pk).timezone, "UTC")


class AdminTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.admin_user = f.UserFactory(is_staff=True, is_superuser=True)
        self.client.force_login(self.admin_user)

    def test_organizations_memberships_and_invitations_are_read_only(self):
        request = RequestFactory().get("/admin/")
        request.user = self.admin_user
        for model in (Organization, OrganizationMembership, OrganizationInvitation):
            model_admin = admin.site._registry[model]
            with self.subTest(model=model.__name__):
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))
                self.assertTrue(model_admin.has_view_permission(request))

    def test_an_edit_posted_anyway_is_refused(self):
        response = self.client.post(
            f"/admin/organizations/organization/{self.org.pk}/change/",
            {"name": "Renamed", "is_suspended": "on"},
        )
        self.assertEqual(response.status_code, 403)
        self.org.refresh_from_db()
        self.assertFalse(self.org.is_suspended)

    def test_the_invitation_token_is_never_shown(self):
        invitation = self.invite("secret@x.test")
        response = self.client.get(
            f"/admin/organizations/organizationinvitation/{invitation.pk}/change/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, invitation.token)

    def test_suspend_and_reactivate_actions_are_audited(self):
        url = "/admin/organizations/organization/"
        response = self.client.post(
            url, {"action": "suspend", "_selected_action": [str(self.org.pk)]}
        )
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertTrue(self.org.is_suspended)
        self.client.post(url, {"action": "reactivate", "_selected_action": [str(self.org.pk)]})
        self.org.refresh_from_db()
        self.assertFalse(self.org.is_suspended)
        actions = set(
            AuditLog.objects.filter(user=self.admin_user).values_list("action", flat=True)
        )
        self.assertTrue({"organization.suspended", "organization.reactivated"} <= actions)


class SeedDemoOutsideDevelopmentTests(TestCase):
    @override_settings(DEBUG=False)
    def test_refused_without_debug(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo", stdout=StringIO())
        self.assertFalse(Organization.objects.exists())
