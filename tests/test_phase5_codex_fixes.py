"""The Phase 5 Codex review (M5.3-M5.5b): one test class per finding."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from bookings.services import create_booking
from organizations.models import OrganizationMembership, OrganizationRole
from saas.flags import is_enabled
from saas.models import FeatureFlag, ImpersonationSession
from scheduling.services import block_time
from staff.forms import BlockTimeForm
from tests import factories as f

PASSWORD = "Str0ng-Passw0rd!"
NEW_YORK = ZoneInfo("America/New_York")


class ImpersonationFixtures:
    def make_fixtures(self):
        cache.clear()
        self.admin = f.UserFactory(email="ops@platform.test", is_platform_staff=True)
        self.admin.set_password(PASSWORD)
        self.admin.save()
        self.spa = f.OrganizationFactory(name="Glow Spa", slug="glow")
        self.clinic = f.OrganizationFactory(name="Bright Clinic", slug="bright")
        self.owner = f.UserFactory(email="sam@example.test", email_verified=True)
        f.MembershipFactory(organization=self.spa, user=self.owner, role=OrganizationRole.OWNER)
        f.MembershipFactory(organization=self.clinic, user=self.owner, role=OrganizationRole.OWNER)

    def start(self, client=None, **extra):
        client = client or self.client
        return client.post(
            reverse("saas-impersonate", args=[self.owner.pk]),
            {
                "organization": self.spa.pk,
                "reason": "Ticket 7",
                "minutes": 30,
                "password": PASSWORD,
                **extra,
            },
        )


class PortalPinningTests(ImpersonationFixtures, TestCase):
    """1. Impersonating someone at one business doesn't open their portal elsewhere."""

    def setUp(self):
        self.make_fixtures()
        # They are also a customer at another business, booked with their verified email.
        self.elsewhere = f.OrganizationFactory(name="Other Studio", slug="studio")
        service = f.ServiceFactory(organization=self.elsewhere)
        provider = f.StaffProfileFactory(organization=self.elsewhere)
        f.make_bookable(provider, service)
        create_booking(
            organization=self.elsewhere,
            service=service,
            staff_profile=provider,
            customer_name="Sam",
            customer_email=self.owner.email,
            start_datetime=f.future(3),
            customer_user=self.owner,
            notify=False,
        )
        self.client.force_login(self.admin)
        self.start()

    def test_other_businesses_portal_and_booking_pages_are_refused(self):
        for url in (
            reverse("portal-organization", args=["studio"]),
            reverse("portal-appointments", args=["studio"]),
            reverse("portal-profile", args=["studio"]),
            reverse("public-booking", args=["studio"]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("portal-profile", args=["studio"]), {}).status_code, 403
        )

    def test_the_portal_overview_shows_only_this_business(self):
        self.assertRedirects(
            self.client.get(reverse("portal-home")),
            reverse("portal-organization", args=["glow"]),
            fetch_redirect_response=False,
        )


class RevalidationTests(ImpersonationFixtures, TestCase):
    """2. Taking away the membership or the organization ends the session; it never falls
    back to another of their organizations."""

    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.admin)
        self.start()

    def assert_revoked(self):
        response = self.client.get(reverse("app-dashboard"))
        self.assertRedirects(response, reverse("saas-dashboard"))
        self.assertEqual(
            ImpersonationSession.objects.get().end_reason,
            ImpersonationSession.EndReason.REVOKED,
        )
        self.assertNotIn("impersonation_session", self.client.session)

    def test_the_membership_is_deactivated(self):
        OrganizationMembership.objects.filter(user=self.owner, organization=self.spa).update(
            is_active=False
        )
        self.assert_revoked()

    def test_the_organization_is_suspended(self):
        self.spa.is_suspended = True
        self.spa.save()
        self.assert_revoked()

    def test_the_target_becomes_platform_staff(self):
        self.owner.is_platform_staff = True
        self.owner.save()
        self.assert_revoked()


class DeactivatedPlatformAccountTests(TestCase):
    """3. Ordinary platform staff can't reactivate a deactivated platform account."""

    def test_reactivation_needs_a_superuser(self):
        operator = f.UserFactory(is_platform_staff=True)
        for flags in ({"is_superuser": True}, {"is_platform_staff": True}):
            with self.subTest(flags=flags):
                account = f.UserFactory(is_active=False, **flags)
                self.client.force_login(operator)
                response = self.client.post(
                    reverse("saas-user", args=[account.pk]),
                    {"action": "reactivate", "reason": "Back"},
                )
                self.assertEqual(response.status_code, 403)
                account.refresh_from_db()
                self.assertFalse(account.is_active)


class ClockChangeTests(TestCase):
    """4. Times the clocks skip are refused, and periods are compared in UTC."""

    def test_a_skipped_time_is_a_form_error(self):
        form = BlockTimeForm(
            {"block-date": "2027-03-14", "block-start": "02:30", "block-end": "03:00"},
            zone=NEW_YORK,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("doesn't exist", " ".join(form.non_field_errors()))

    def test_a_real_time_across_the_change_is_converted_to_utc(self):
        form = BlockTimeForm(
            {"block-date": "2027-03-14", "block-start": "01:30", "block-end": "03:15"},
            zone=NEW_YORK,
        )
        self.assertTrue(form.is_valid(), form.errors)
        start, end = form.period()
        # 01:30 EST is 06:30 UTC; 03:15 EDT is 07:15 UTC: 45 minutes, not 1 h 45.
        self.assertEqual((start.hour, start.minute, end.hour, end.minute), (6, 30, 7, 15))

    def test_the_service_compares_in_utc(self):
        organization = f.OrganizationFactory()
        provider = f.StaffProfileFactory(organization=organization)
        # Wall clock says 02:30 < 03:00, but 02:30 doesn't exist: in UTC it ends first.
        with self.assertRaises(ValidationError):
            block_time(
                staff=provider,
                start=datetime.combine(date(2027, 3, 14), time(2, 30), tzinfo=NEW_YORK),
                end=datetime.combine(date(2027, 3, 14), time(3, 0), tzinfo=NEW_YORK),
            )


class FlagRenameTests(TestCase):
    """5. Renaming a flag stops the old name answering from the cache."""

    def test_the_old_key_is_forgotten(self):
        cache.clear()
        admin = f.UserFactory(is_platform_staff=True)
        organization = f.OrganizationFactory()
        flag = FeatureFlag.objects.create(key="old-key", enabled=True)
        self.assertTrue(is_enabled("old-key", organization))  # cached now
        self.client.force_login(admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("saas-flag", args=[flag.pk]),
                {"key": "new-key", "description": "", "enabled": "on"},
            )
        self.assertFalse(is_enabled("old-key", organization))
        self.assertTrue(is_enabled("new-key", organization))


class LogoutCsrfTests(ImpersonationFixtures, TestCase):
    """6. Ending an impersonation by signing out needs a valid CSRF token."""

    def test_without_a_token_nothing_ends(self):
        self.make_fixtures()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        page = client.get(reverse("saas-impersonate", args=[self.owner.pk]))
        token = page.cookies["csrftoken"].value
        self.start(client, csrfmiddlewaretoken=token)
        session = ImpersonationSession.objects.get()
        self.assertEqual(client.post(reverse("logout")).status_code, 403)
        session.refresh_from_db()
        self.assertIsNone(session.ended_at)
        response = client.post(reverse("logout"), {"csrfmiddlewaretoken": token})
        self.assertRedirects(response, reverse("saas-user", args=[self.owner.pk]))
        session.refresh_from_db()
        self.assertEqual(session.end_reason, ImpersonationSession.EndReason.ENDED)
        self.assertEqual(client.get(reverse("saas-dashboard")).status_code, 200)
