"""M2.5: the web app shell. Session sign-in, role-based home pages, capability-driven
navigation (a hidden link and a 403 page always agree), account pages, the organization
switcher, htmx partials and the Content Security Policy."""

import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import EmailVerificationToken, LoginHistory
from core.audit import AuditAction, record_audit
from core.navigation import APP_NAVIGATION
from organizations.models import OrganizationMembership, OrganizationRole
from organizations.permissions import Capability
from organizations.tenancy import SESSION_KEY
from tests import factories as f

User = get_user_model()
PASSWORD = f.DEFAULT_PASSWORD
TEAM = [
    OrganizationRole.OWNER,
    OrganizationRole.MANAGER,
    OrganizationRole.RECEPTIONIST,
    OrganizationRole.STAFF,
]


def member(role, organization=None, **kwargs):
    organization = organization or f.OrganizationFactory()
    return f.MembershipFactory(organization=organization, role=role, **kwargs).user


class LoginLogoutTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = member(OrganizationRole.MANAGER)

    def login(self, email, password=PASSWORD, **extra):
        return self.client.post(
            reverse("login"), {"username": email, "password": password}, **extra
        )

    def test_sign_in_by_email_ignoring_case(self):
        response = self.login(self.user.email.upper())
        self.assertRedirects(response, reverse("home"), target_status_code=302)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertTrue(LoginHistory.objects.filter(user=self.user, is_successful=True).exists())

    def test_wrong_password_is_refused_and_recorded(self):
        response = self.login(self.user.email, "not-the-password")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "don&#x27;t match an account")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(LoginHistory.objects.filter(user=self.user, is_successful=False).exists())

    def test_next_must_stay_on_this_site(self):
        response = self.client.post(
            f"{reverse('login')}?next=https://evil.example/",
            {"username": self.user.email, "password": PASSWORD, "next": "https://evil.example/"},
        )
        self.assertEqual(response["Location"], reverse("home"))

    def test_next_on_this_site_is_honoured(self):
        response = self.client.post(
            reverse("login"),
            {"username": self.user.email, "password": PASSWORD, "next": reverse("app-team")},
        )
        self.assertEqual(response["Location"], reverse("app-team"))

    @override_settings(WEB_LOGIN_RATE="3/900")
    def test_attempts_are_rate_limited_even_with_the_right_password(self):
        for _ in range(3):
            self.login(self.user.email, "wrong-password")
        response = self.login(self.user.email)
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(WEB_LOGIN_RATE="3/900")
    def test_rate_limit_is_per_email_as_well_as_per_address(self):
        for n in range(3):
            self.login(self.user.email, "wrong", REMOTE_ADDR=f"10.0.0.{n}")
        self.assertEqual(self.login(self.user.email, REMOTE_ADDR="10.0.0.9").status_code, 429)

    def test_sign_out_needs_post(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signed_in_user_skips_the_login_page(self):
        self.client.force_login(self.user)
        self.assertRedirects(
            self.client.get(reverse("login")), reverse("home"), target_status_code=302
        )


class RoleHomeTests(TestCase):
    def home_for(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("home"))

    def test_each_role_lands_on_its_home(self):
        expected = {
            OrganizationRole.OWNER: "app-dashboard",
            OrganizationRole.MANAGER: "app-dashboard",
            OrganizationRole.RECEPTIONIST: "app-reception",
            OrganizationRole.STAFF: "staff-dashboard",
            OrganizationRole.CUSTOMER: "portal-home",
        }
        for role, url_name in expected.items():
            with self.subTest(role=role):
                response = self.home_for(member(role))
                self.assertRedirects(response, reverse(url_name))

    def test_account_without_organization_goes_to_the_portal(self):
        self.assertRedirects(self.home_for(f.UserFactory()), reverse("portal-home"))

    def test_platform_admin_goes_to_the_platform(self):
        admin = User.objects.create_superuser(email="root@example.test", password=PASSWORD)
        self.assertRedirects(self.home_for(admin), reverse("saas-dashboard"))

    def test_anonymous_visitors_are_sent_to_sign_in(self):
        for url_name in ("home", "app-dashboard", "app-team", "staff-dashboard", "portal-home"):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("login")))


class NavigationMatchesAccessTests(TestCase):
    """Every sidebar link is shown exactly when its page is allowed."""

    def test_links_and_pages_agree_for_every_role(self):
        organization = f.OrganizationFactory()
        items = [item for section in APP_NAVIGATION for item in section.items]
        for role in TEAM:
            user = member(role, organization)
            self.client.force_login(user)
            dashboard = self.client.get(reverse("app-dashboard"))
            self.assertEqual(dashboard.status_code, 200)
            for item in items:
                with self.subTest(role=role, page=item.url_name):
                    url = reverse(item.url_name)
                    shown = f'href="{url}"' in dashboard.content.decode()
                    status = self.client.get(url).status_code
                    self.assertIn(status, (200, 403))
                    self.assertEqual(shown, status == 200)

    def test_restricted_pages(self):
        organization = f.OrganizationFactory()
        cases = [
            (OrganizationRole.RECEPTIONIST, "app-team", 403),
            (OrganizationRole.STAFF, "app-team", 403),
            (OrganizationRole.MANAGER, "app-team", 200),
            (OrganizationRole.MANAGER, "app-audit-log", 403),
            (OrganizationRole.OWNER, "app-audit-log", 200),
        ]
        for role, url_name, status in cases:
            with self.subTest(role=role, page=url_name):
                self.client.force_login(member(role, organization))
                self.assertEqual(self.client.get(reverse(url_name)).status_code, status)

    def test_capabilities_not_role_names_decide(self):
        organization = f.OrganizationFactory()
        granted = member(
            OrganizationRole.STAFF, organization, granted_permissions=[Capability.MEMBERS_VIEW]
        )
        revoked = member(
            OrganizationRole.OWNER, organization, revoked_permissions=[Capability.AUDIT_VIEW]
        )
        self.client.force_login(granted)
        self.assertContains(self.client.get(reverse("app-dashboard")), reverse("app-team"))
        self.assertEqual(self.client.get(reverse("app-team")).status_code, 200)
        self.client.force_login(revoked)
        self.assertNotContains(self.client.get(reverse("app-dashboard")), reverse("app-audit-log"))
        self.assertEqual(self.client.get(reverse("app-audit-log")).status_code, 403)

    def test_customers_and_outsiders_cannot_open_the_app(self):
        for user in (member(OrganizationRole.CUSTOMER), f.UserFactory()):
            self.client.force_login(user)
            for url_name in ("app-dashboard", "staff-dashboard", "app-team"):
                with self.subTest(user=user.email, page=url_name):
                    self.assertEqual(self.client.get(reverse(url_name)).status_code, 403)

    def test_platform_pages_are_for_superusers_only(self):
        self.client.force_login(member(OrganizationRole.OWNER))
        self.assertEqual(self.client.get(reverse("saas-dashboard")).status_code, 403)
        admin = User.objects.create_superuser(email="root@example.test", password=PASSWORD)
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("saas-dashboard")).status_code, 200)
        # No implicit tenant access for the platform admin.
        self.assertEqual(self.client.get(reverse("app-dashboard")).status_code, 403)

    def test_component_gallery_is_development_only(self):
        self.client.force_login(member(OrganizationRole.OWNER))
        self.assertEqual(self.client.get(reverse("app-components")).status_code, 404)
        with override_settings(DEBUG=True):
            self.assertEqual(self.client.get(reverse("app-components")).status_code, 200)


class TenantIsolationOnPagesTests(TestCase):
    def setUp(self):
        self.org_a, self.org_b = f.OrganizationFactory(), f.OrganizationFactory()
        self.owner_a = member(OrganizationRole.OWNER, self.org_a)
        self.owner_b = member(OrganizationRole.OWNER, self.org_b)
        self.client.force_login(self.owner_a)

    def test_team_page_lists_only_own_members(self):
        response = self.client.get(reverse("app-team"))
        self.assertContains(response, self.owner_a.email)
        self.assertNotContains(response, self.owner_b.email)

    def test_audit_log_lists_only_own_entries(self):
        record_audit(AuditAction.SYSTEM_TEST, organization=self.org_b, actor=self.owner_b)
        record_audit(AuditAction.TAG_CREATED, organization=self.org_a, actor=self.owner_a)
        response = self.client.get(reverse("app-audit-log"))
        self.assertContains(response, "tag.created")
        self.assertNotContains(response, "system.test")

    def test_a_foreign_organization_slug_grants_nothing(self):
        response = self.client.get(reverse("app-team"), {"organization": self.org_b.slug})
        self.assertEqual(response.status_code, 403)

    def test_suspended_organization_is_a_403_page(self):
        self.org_a.is_suspended = True
        self.org_a.save()
        session = self.client.session
        session[SESSION_KEY] = str(self.org_a.pk)
        session.save()
        response = self.client.get(reverse("app-dashboard"))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "suspended", status_code=403)

    def test_dashboard_lists_todays_appointments_of_own_organization_only(self):
        now = timezone.now()
        own = f.BookingFactory(organization=self.org_a, start_datetime=now)
        other = f.BookingFactory(organization=self.org_b, start_datetime=now)
        response = self.client.get(reverse("app-dashboard"))
        self.assertContains(response, own.customer_name)
        self.assertNotContains(response, other.customer_name)

    def test_page_time_zone_does_not_leak_into_later_requests(self):
        self.org_a.timezone = "Asia/Tokyo"
        self.org_a.save()
        self.client.get(reverse("app-dashboard"))
        self.assertEqual(timezone.get_current_timezone_name(), settings.TIME_ZONE)


class StaffAndPortalPageTests(TestCase):
    def test_staff_see_only_their_own_schedule(self):
        organization = f.OrganizationFactory()
        provider = f.StaffProfileFactory(organization=organization)
        f.MembershipFactory(
            organization=organization, user=provider.user, role=OrganizationRole.STAFF
        )
        mine = f.BookingFactory(organization=organization, staff=provider)
        theirs = f.BookingFactory(organization=organization)
        self.client.force_login(provider.user)
        response = self.client.get(reverse("staff-dashboard"))
        self.assertContains(response, mine.customer_name)
        self.assertNotContains(response, theirs.customer_name)

    def test_portal_lists_only_the_customers_own_bookings(self):
        user = f.UserFactory(email_verified=True)
        mine = f.BookingFactory(customer=f.CustomerFactory(user=user))
        theirs = f.BookingFactory()
        self.client.force_login(user)
        response = self.client.get(reverse("portal-home"))
        self.assertContains(response, mine.reference)
        self.assertNotContains(response, theirs.reference)


class OrganizationSwitcherTests(TestCase):
    def setUp(self):
        self.user = f.UserFactory()
        self.org_a = f.OrganizationFactory(name="Alpha Clinic")
        self.org_b = f.OrganizationFactory(name="Beta Spa")
        for organization in (self.org_a, self.org_b):
            f.MembershipFactory(organization=organization, user=self.user)
        self.client.force_login(self.user)

    def test_switch_between_own_organizations(self):
        self.assertContains(self.client.get(reverse("app-dashboard")), "Beta Spa")  # menu entry
        response = self.client.post(reverse("switch-organization", args=[self.org_b.slug]))
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertEqual(self.client.session[SESSION_KEY], str(self.org_b.pk))
        page = self.client.get(reverse("app-team"))
        self.assertEqual(page.context["organization"], self.org_b)

    def test_cannot_switch_into_a_foreign_organization(self):
        foreign = f.OrganizationFactory()
        self.client.post(reverse("switch-organization", args=[foreign.slug]))
        self.assertNotEqual(self.client.session.get(SESSION_KEY), str(foreign.pk))

    def test_switching_needs_post_and_a_safe_next(self):
        url = reverse("switch-organization", args=[self.org_b.slug])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, {"next": "https://evil.example/"})
        self.assertEqual(response["Location"], reverse("home"))


class HtmxPartialTests(TestCase):
    def setUp(self):
        organization = f.OrganizationFactory()
        self.owner = member(OrganizationRole.OWNER, organization)
        self.other = member(OrganizationRole.STAFF, organization)
        self.client.force_login(self.owner)

    def test_htmx_request_gets_only_the_results(self):
        response = self.client.get(
            reverse("app-team"), {"q": self.other.email}, headers={"HX-Request": "true"}
        )
        body = response.content.decode()
        self.assertNotIn("<html", body)
        self.assertIn(self.other.email, body)
        self.assertNotIn(self.owner.email, body)
        self.assertIn("HX-Request", response["Vary"])

    def test_history_restore_gets_the_full_page(self):
        response = self.client.get(
            reverse("app-team"),
            headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        )
        self.assertContains(response, "<html")

    def test_role_filter(self):
        response = self.client.get(reverse("app-team"), {"role": OrganizationRole.STAFF})
        self.assertContains(response, self.other.email)
        self.assertNotContains(response, f"<td>{self.owner.email}</td>")


class PasswordResetPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = f.UserFactory()

    @mock.patch("accounts.services.send_password_reset_email.delay")
    def test_request_does_the_same_work_and_answer_for_any_address(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            known = self.client.post(reverse("password-reset"), {"email": self.user.email})
            unknown = self.client.post(reverse("password-reset"), {"email": "nobody@example.test"})
        # The worker decides whether there is an account to email (accounts.tasks).
        self.assertEqual(
            [call.kwargs for call in delay.call_args_list],
            [{"email": self.user.email}, {"email": "nobody@example.test"}],
        )
        self.assertEqual(known["Location"], unknown["Location"])

    @override_settings(WEB_PASSWORD_RESET_RATE="2/3600")
    def test_requests_are_rate_limited(self):
        for _ in range(2):
            self.client.post(reverse("password-reset"), {"email": self.user.email})
        response = self.client.post(reverse("password-reset"), {"email": self.user.email})
        self.assertEqual(response.status_code, 429)

    def reset_url(self, token=None):
        token = token or default_token_generator.make_token(self.user)
        return f"{reverse('password-reset-confirm')}?uid={self.user.pk}&token={token}"

    def test_emailed_link_sets_a_new_password_and_signs_out_everywhere(self):
        refresh = RefreshToken.for_user(self.user)
        url = self.reset_url()
        self.assertEqual(self.client.get(url).status_code, 200)
        new_password = "Fresh-Passphrase-2026"
        response = self.client.post(
            url, {"new_password1": new_password, "new_password2": new_password}
        )
        self.assertRedirects(response, reverse("login"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))
        self.assertTrue(BlacklistedToken.objects.filter(token__jti=refresh["jti"]).exists())
        # The link works once.
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_weak_or_mismatched_passwords_are_refused(self):
        url = self.reset_url()
        for first, second in (("short", "short"), ("Fresh-Passphrase-1", "Different-Pass-2")):
            with self.subTest(first=first):
                response = self.client.post(url, {"new_password1": first, "new_password2": second})
                self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_bad_link_is_refused(self):
        self.assertEqual(self.client.get(self.reset_url("bad-token")).status_code, 400)
        self.assertEqual(
            self.client.get(f"{reverse('password-reset-confirm')}?uid=abc").status_code, 400
        )


class VerifyEmailPageTests(TestCase):
    def setUp(self):
        self.user = f.UserFactory(email_verified=False)
        self.token = EmailVerificationToken.objects.create(
            user=self.user,
            token=EmailVerificationToken.generate_token(),
            expires_at=EmailVerificationToken.default_expires_at(),
        )
        self.url = f"{reverse('verify-email')}?token={self.token.token}"

    def test_opening_the_link_does_not_verify_but_confirming_does(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        self.assertRedirects(self.client.post(self.url), reverse("login"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        self.assertEqual(self.client.post(self.url).status_code, 400)  # used

    def test_unknown_token(self):
        self.assertEqual(self.client.get(f"{reverse('verify-email')}?token=x").status_code, 400)


class AcceptInvitationPageTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory(name="Harbour Physio")
        self.invitation = f.InvitationFactory(
            organization=self.organization, role=OrganizationRole.RECEPTIONIST
        )
        self.url = f"{reverse('accept-invitation')}?token={self.invitation.token}"
        self.signup = {
            "first_name": "Rowan",
            "last_name": "Hale",
            "new_password1": "Harbour-Physio-2026",
            "new_password2": "Harbour-Physio-2026",
            "accept_terms": "on",
        }

    def test_new_person_creates_an_account_and_joins(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Create account and join")
        response = self.client.post(self.url, self.signup)
        self.assertRedirects(response, reverse("home"), target_status_code=302)
        user = User.objects.get(email=self.invitation.email)
        self.assertTrue(user.email_verified)
        membership = OrganizationMembership.objects.get(user=user)
        self.assertEqual(
            (membership.organization, membership.role),
            (self.organization, OrganizationRole.RECEPTIONIST),
        )
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_weak_password_creates_nothing(self):
        data = self.signup | {"new_password1": "123", "new_password2": "123"}
        self.assertEqual(self.client.post(self.url, data).status_code, 400)
        self.assertFalse(User.objects.filter(email=self.invitation.email).exists())

    def test_existing_account_must_sign_in_first(self):
        f.UserFactory(email=self.invitation.email)
        response = self.client.get(self.url)
        self.assertContains(response, "Sign in to accept")
        self.assertEqual(self.client.post(self.url, self.signup).status_code, 400)
        self.assertFalse(OrganizationMembership.objects.filter(organization=self.organization))

    def test_signed_in_invitee_confirms(self):
        user = f.UserFactory(email=self.invitation.email)
        self.client.force_login(user)
        self.assertContains(self.client.get(self.url), "Accept invitation")
        self.client.post(self.url)
        self.assertTrue(
            OrganizationMembership.objects.filter(user=user, organization=self.organization)
        )
        self.assertEqual(self.client.session[SESSION_KEY], str(self.organization.pk))

    def test_a_different_account_cannot_accept(self):
        self.client.force_login(f.UserFactory())
        self.assertContains(self.client.get(self.url), "different email address")
        self.assertEqual(self.client.post(self.url).status_code, 400)
        self.assertFalse(OrganizationMembership.objects.filter(organization=self.organization))

    def test_used_or_unknown_invitation(self):
        self.invitation.accepted_at = timezone.now()
        self.invitation.save()
        self.assertEqual(self.client.get(self.url).status_code, 400)
        response = self.client.get(f"{reverse('accept-invitation')}?token=nope")
        self.assertEqual(response.status_code, 400)


class SecurityHeaderTests(TestCase):
    def test_pages_send_a_strict_content_security_policy(self):
        policy = self.client.get(reverse("login"))["Content-Security-Policy"]
        self.assertIn("script-src 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn("unsafe", policy)

    def test_api_docs_get_their_own_policy(self):
        policy = self.client.get(reverse("swagger-ui"))["Content-Security-Policy"]
        self.assertIn("'unsafe-inline'", policy)

    def test_pages_load_only_self_hosted_assets(self):
        for url_name in ("landing", "login", "password-reset"):
            with self.subTest(page=url_name):
                html = self.client.get(reverse(url_name)).content.decode()
                sources = re.findall(r'<(?:script|link)[^>]+(?:src|href)="([^"]+)"', html)
                self.assertTrue(sources)
                for source in sources:
                    self.assertTrue(source.startswith(settings.STATIC_URL), source)
                self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)(?![^>]*application/json)")

    def test_pages_show_no_template_comment_markup(self):
        # {# #} comments are single-line; a multi-line one is printed into the page.
        for url_name in ("landing", "login"):
            with self.subTest(page=url_name):
                self.assertNotContains(self.client.get(reverse(url_name)), "{#")

    def test_every_referenced_static_file_exists(self):
        # A file missing from the checkout (e.g. ignored by .gitignore) fails here in CI.
        from django.contrib.staticfiles import finders

        for path in (
            "dist/app.css",
            "js/app.js",
            "favicon.svg",
            "vendor/htmx-2.0.11.min.js",
            "vendor/alpine-csp-3.17.4.min.js",
            "vendor/alpine-focus-3.17.4.min.js",
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(finders.find(path))

    def test_no_template_references_a_cdn(self):
        root = Path(settings.BASE_DIR) / "templates"
        for template in root.rglob("*.html"):
            text = template.read_text(encoding="utf-8")
            with self.subTest(template=str(template.relative_to(root))):
                self.assertNotRegex(text, r"<(script|link)[^>]+(src|href)=\"(https?:)?//")

    def test_htmx_sends_the_csrf_token_from_the_page(self):
        self.client.force_login(member(OrganizationRole.OWNER))
        response = self.client.get(reverse("app-dashboard"))
        self.assertContains(response, 'hx-headers=\'{"X-CSRFToken": ')
        self.assertTrue(settings.CSRF_COOKIE_HTTPONLY)


class ErrorPageTests(TestCase):
    def test_not_found_page_uses_the_layout(self):
        response = self.client.get("/no-such-page/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Page not found", status_code=404)


class DashboardSummaryTimeZoneTests(TestCase):
    def test_periods_are_calendar_days_in_the_organization_time_zone(self):
        from datetime import datetime, time, timedelta
        from zoneinfo import ZoneInfo

        from bookings.models import Booking
        from dashboard.selectors import organization_dashboard_summary

        organization = f.OrganizationFactory(timezone="Pacific/Pago_Pago")  # UTC-11
        zone = ZoneInfo(organization.timezone)
        today = timezone.now().astimezone(zone).date()
        noon = datetime.combine(today, time(12), tzinfo=zone)
        f.BookingFactory(organization=organization, start_datetime=noon)
        f.BookingFactory(
            organization=organization, start_datetime=noon, status=Booking.Status.CANCELLED
        )
        f.BookingFactory(organization=organization, start_datetime=noon + timedelta(days=1))
        f.BookingFactory(organization=organization, start_datetime=noon + timedelta(days=40))

        summary = organization_dashboard_summary(organization)
        self.assertEqual(summary["appointments_today"], 1)
        self.assertLessEqual(summary["appointments_this_week"], 2)
        self.assertLessEqual(summary["appointments_this_month"], 2)
        self.assertEqual(summary["cancelled_bookings"], 1)

        owner = member(OrganizationRole.OWNER, organization)
        self.client.force_login(owner)
        response = self.client.get(reverse("app-dashboard"))
        self.assertEqual(response.context["report"]["today"], summary["appointments_today"])
