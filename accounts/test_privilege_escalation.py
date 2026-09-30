"""Privilege rules ported from the legacy role tests (M1.6), restated for memberships.

Account endpoints can never grant platform flags or tenant access, and access is re-checked
on every request, so removing a membership takes effect even for an already-issued JWT.
"""

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from organizations.models import OrganizationMembership, OrganizationRole
from tests import factories as f

User = get_user_model()
ELEVATION = {
    "role": "owner",
    "is_staff": True,
    "is_superuser": True,
    "is_platform_admin": True,
    "is_platform_staff": True,
}


class PrivilegeEscalationTests(APITestCase):
    def test_registration_cannot_grant_platform_flags_or_tenant_access(self):
        f.OrganizationFactory()
        response = self.client.post(
            "/api/register/",
            {
                "email": "new-user@example.test",
                "password": "StrongNewPass123",
                "accept_terms": True,
                "accept_privacy": True,
                **ELEVATION,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(email="new-user@example.test")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_platform_staff)
        self.assertFalse(OrganizationMembership.objects.filter(user=user).exists())
        self.client.force_authenticate(user)
        self.assertEqual(self.client.get("/api/v1/customers/").status_code, 403)

    def test_profile_updates_cannot_grant_platform_flags(self):
        user = f.UserFactory()
        self.client.force_authenticate(user)
        for method in ("patch", "put"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    "/api/profile/", {"first_name": "Allowed edit", **ELEVATION}, format="json"
                )
                self.assertEqual(response.status_code, 200, response.data)
                user.refresh_from_db()
                self.assertEqual(user.first_name, "Allowed edit")
                self.assertFalse(user.is_staff)
                self.assertFalse(user.is_superuser)
                self.assertFalse(user.is_platform_staff)

    def test_removed_membership_takes_effect_for_an_existing_jwt(self):
        membership = f.MembershipFactory(role=OrganizationRole.MANAGER)
        response = self.client.post(
            "/api/login/",
            {"email": membership.user.email, "password": f.DEFAULT_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        self.assertEqual(self.client.get("/api/v1/customers/").status_code, 200)

        membership.delete()
        self.assertEqual(self.client.get("/api/v1/customers/").status_code, 403)

    def test_revoked_capability_takes_effect_for_an_existing_jwt(self):
        membership = f.MembershipFactory(role=OrganizationRole.MANAGER)
        response = self.client.post(
            "/api/login/",
            {"email": membership.user.email, "password": f.DEFAULT_PASSWORD},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        self.assertEqual(self.client.get("/api/v1/customers/").status_code, 200)

        membership.revoked_permissions = ["customers.view"]
        membership.save(update_fields=["revoked_permissions"])
        self.assertEqual(self.client.get("/api/v1/customers/").status_code, 403)
