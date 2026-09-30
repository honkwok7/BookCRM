"""M5.5b: impersonation (step-up, time-boxed, banner, audited, blocked areas), announcements
and feature flags."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bookings.models import Booking
from core.audit import AuditAction
from core.models import AuditLog
from organizations.models import OrganizationRole
from saas.flags import is_enabled
from saas.models import Announcement, FeatureFlag, ImpersonationSession
from tests import factories as f

User = get_user_model()
PASSWORD = "Str0ng-Passw0rd!"


class Fixtures:
    def make_fixtures(self):
        cache.clear()
        self.admin = f.UserFactory(email="ops@platform.test", is_platform_staff=True)
        self.admin.set_password(PASSWORD)
        self.admin.save()
        self.org = f.OrganizationFactory(name="Glow Spa", slug="glow")
        self.other_org = f.OrganizationFactory(name="Other", slug="other")
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        f.MembershipFactory(
            organization=self.other_org, user=self.owner, role=OrganizationRole.OWNER
        )
        self.client.force_login(self.admin)

    def start(self, **overrides):
        data = {
            "organization": self.org.pk,
            "reason": "Ticket 42",
            "minutes": 30,
            "password": PASSWORD,
            **overrides,
        }
        return self.client.post(reverse("saas-impersonate", args=[self.owner.pk]), data)

    def session(self):
        return ImpersonationSession.objects.get()


class StartTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_it_needs_the_admins_password_and_a_reason(self):
        self.assertEqual(self.start(password="wrong").status_code, 403)
        self.assertEqual(self.start(reason="").status_code, 422)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_platform_accounts_cannot_be_impersonated(self):
        colleague = f.UserFactory(is_platform_staff=True)
        f.MembershipFactory(organization=self.org, user=colleague)
        response = self.client.post(
            reverse("saas-impersonate", args=[colleague.pk]),
            {"organization": self.org.pk, "reason": "x", "minutes": 30, "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_only_their_own_organizations_are_offered(self):
        stranger_org = f.OrganizationFactory()
        self.assertEqual(self.start(organization=stranger_org.pk).status_code, 422)

    def test_start_shows_the_app_as_them_with_a_banner(self):
        self.assertRedirects(self.start(), reverse("home"), fetch_redirect_response=False)
        session = self.session()
        self.assertEqual(
            (session.admin, session.target_user, session.reason),
            (self.admin, self.owner, "Ticket 42"),
        )
        self.assertAlmostEqual(
            (session.expires_at - session.started_at).total_seconds(), 30 * 60, delta=1
        )
        response = self.client.get(reverse("app-dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["user"], self.owner)
        self.assertEqual(response.context["tenant"].organization, self.org)
        self.assertContains(response, f"Impersonating {self.owner.email}")
        started = AuditLog.objects.get(action=AuditAction.IMPERSONATION_STARTED)
        self.assertEqual(
            (started.user, started.actor_type), (self.admin, AuditLog.ActorType.PLATFORM_ADMIN)
        )


class WhileImpersonatingTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.start()

    def test_changes_are_audited_with_the_impersonator(self):
        booking = f.BookingFactory(organization=self.org, start_datetime=f.future(3))
        response = self.client.post(
            reverse("app-appointment-action", args=[booking.pk]),
            {"action": "cancel", "reason": "Asked by phone"},
        )
        self.assertIn(response.status_code, (200, 302))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        cancelled = AuditLog.objects.get(action=AuditAction.BOOKING_CANCELLED)
        self.assertEqual((cancelled.user, cancelled.impersonator), (self.owner, self.admin))
        request_row = AuditLog.objects.filter(action=AuditAction.IMPERSONATION_REQUEST).first()
        self.assertEqual(
            (request_row.user, request_row.impersonator, request_row.metadata["method"]),
            (self.owner, self.admin, "POST"),
        )

    def test_blocked_areas(self):
        for url in (
            reverse("saas-dashboard"),
            "/api/v1/customers/",
            "/admin/",
            reverse("switch-organization", args=[self.other_org.slug]),
            reverse("app-dashboard") + f"?organization={self.other_org.slug}",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_signing_out_ends_it_and_keeps_the_admin_signed_in(self):
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("saas-user", args=[self.owner.pk]))
        self.assertEqual(self.session().end_reason, ImpersonationSession.EndReason.ENDED)
        self.assertEqual(self.client.get(reverse("saas-dashboard")).status_code, 200)

    def test_the_end_button(self):
        response = self.client.post(reverse("impersonation-end"))
        self.assertRedirects(response, reverse("saas-user", args=[self.owner.pk]))
        self.assertIsNotNone(self.session().ended_at)
        response = self.client.get(reverse("saas-dashboard"))
        self.assertNotContains(response, "Impersonating")
        ended = AuditLog.objects.get(action=AuditAction.IMPERSONATION_ENDED)
        self.assertEqual((ended.user, ended.impersonator), (self.admin, None))

    def test_it_expires(self):
        ImpersonationSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.get(reverse("app-dashboard"))
        self.assertRedirects(response, reverse("saas-dashboard"))
        self.assertEqual(self.session().end_reason, ImpersonationSession.EndReason.EXPIRED)
        self.assertEqual(self.client.get(reverse("saas-dashboard")).status_code, 200)

    def test_it_stops_when_the_target_is_deactivated(self):
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.client.get(reverse("app-dashboard"))
        self.assertEqual(self.session().end_reason, ImpersonationSession.EndReason.REVOKED)


class AnnouncementTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def announce(self, **fields):
        data = {
            "title": "Maintenance tonight",
            "body": "Short downtime at 02:00 UTC.",
            "audience": Announcement.Audience.TEAMS,
            "level": Announcement.Level.WARNING,
            "starts_at": (timezone.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
            "ends_at": "",
            "is_active": "on",
            **fields,
        }
        return self.client.post(reverse("saas-announcement-new"), data)

    def banner(self, user, where="app"):
        self.client.force_login(user)
        return self.client.get(reverse("announcements"), {"where": where})

    def test_teams_see_it_customers_do_not(self):
        self.assertRedirects(self.announce(), reverse("saas-announcements"))
        self.assertContains(self.banner(self.owner), "Maintenance tonight")
        customer = f.UserFactory()
        self.assertNotContains(self.banner(customer, "portal"), "Maintenance tonight")
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.ANNOUNCEMENT_SAVED).exists())

    def test_dismiss_and_end(self):
        self.announce()
        announcement = Announcement.objects.get()
        self.client.force_login(self.owner)
        self.client.post(reverse("announcement-dismiss", args=[announcement.pk]))
        self.assertNotContains(self.banner(self.owner), "Maintenance tonight")
        # A new session sees it again until it ends.
        self.client.logout()
        self.assertContains(self.banner(self.owner), "Maintenance tonight")
        Announcement.objects.update(ends_at=timezone.now() - timedelta(seconds=1))
        cache.clear()
        self.assertNotContains(self.banner(self.owner), "Maintenance tonight")

    def test_owners_only_and_customers(self):
        self.announce(title="For owners", audience=Announcement.Audience.OWNERS)
        self.announce(title="For customers", audience=Announcement.Audience.CUSTOMERS)
        receptionist = f.MembershipFactory(
            organization=self.org, role=OrganizationRole.RECEPTIONIST
        ).user
        self.assertContains(self.banner(self.owner), "For owners")
        self.assertNotContains(self.banner(receptionist), "For owners")
        self.assertContains(self.banner(receptionist, "portal"), "For customers")
        self.assertNotContains(self.banner(receptionist), "For customers")

    def test_the_end_must_follow_the_start(self):
        response = self.announce(
            ends_at=(timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        )
        self.assertEqual(response.status_code, 422)


class FlagTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_global_setting_overrides_and_cache(self):
        self.assertFalse(is_enabled("new-calendar", self.org))  # unknown: off
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("saas-flags"), {"key": "new-calendar", "description": "", "enabled": "on"}
            )
        flag = FeatureFlag.objects.get(key="new-calendar")
        self.assertRedirects(response, reverse("saas-flag", args=[flag.pk]))
        self.assertTrue(is_enabled("new-calendar", self.org))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("saas-flag", args=[flag.pk]), {"organization": "glow", "state": "off"}
            )
        self.assertFalse(is_enabled("new-calendar", self.org))
        self.assertTrue(is_enabled("new-calendar", self.other_org))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("saas-flag", args=[flag.pk]), {"organization": "glow", "state": "default"}
            )
        self.assertTrue(is_enabled("new-calendar", self.org))
        self.assertEqual(
            AuditLog.objects.filter(action=AuditAction.FEATURE_FLAG_CHANGED).count(), 3
        )

    def test_an_unknown_organization_is_an_error(self):
        flag = FeatureFlag.objects.create(key="beta")
        response = self.client.post(
            reverse("saas-flag", args=[flag.pk]), {"organization": "nope", "state": "on"}
        )
        self.assertEqual(response.status_code, 422)
