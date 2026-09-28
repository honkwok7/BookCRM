from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from organizations.services import accept_invitation

User = get_user_model()


class InvitationTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Org A", slug="org-a")
        self.owner = User.objects.create_user(email="owner@orga.com", password="Owner12345!")
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.owner,
            role=OrganizationRole.OWNER,
            is_active=True,
        )

    def test_accept_valid_invitation(self):
        invited = User.objects.create_user(email="staff@orga.com", password="Staff12345!")
        invitation = OrganizationInvitation.objects.create(
            organization=self.organization,
            email=invited.email,
            role=OrganizationRole.STAFF,
            token=OrganizationInvitation.generate_token(),
            expires_at=timezone.now() + timedelta(days=1),
            inviter=self.owner,
        )
        membership = accept_invitation(invitation=invitation, user=invited)
        self.assertEqual(membership.role, OrganizationRole.STAFF)

    def test_expired_invitation_rejected(self):
        invited = User.objects.create_user(email="late@orga.com", password="Late12345!")
        invitation = OrganizationInvitation.objects.create(
            organization=self.organization,
            email=invited.email,
            role=OrganizationRole.STAFF,
            token=OrganizationInvitation.generate_token(),
            expires_at=timezone.now() - timedelta(minutes=1),
            inviter=self.owner,
        )
        with self.assertRaises(ValueError):
            accept_invitation(invitation=invitation, user=invited)
