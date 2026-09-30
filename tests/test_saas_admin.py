"""M5.5a: the platform admin pages - access, the overview's numbers and alerts, organizations
(create, suspend, reactivate), subscriptions, plans, accounts and the audit log. Everything is
audited as a platform action, and no organization's customers or appointments are shown."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.audit import AuditAction, record_audit
from core.models import AuditLog
from locations.models import Location
from notifications.models import NotificationLog
from organizations.models import Organization, OrganizationInvitation, OrganizationRole
from saas.metrics import platform_overview
from saas.tasks import HEARTBEAT_KEY, heartbeat
from subscriptions.models import Plan, Subscription
from tests import factories as f

User = get_user_model()
PASSWORD = "Str0ng-Passw0rd!"


def plan(slug, monthly, yearly):
    return Plan.objects.create(
        name=slug.title(), slug=slug, monthly_price=monthly, yearly_price=yearly
    )


def subscribe(organization, plan, status, cycle=Subscription.BillingCycle.MONTHLY):
    return Subscription.objects.update_or_create(
        organization=organization,
        defaults={"plan": plan, "status": status, "billing_cycle": cycle},
    )[0]


class Fixtures:
    def make_fixtures(self):
        cache.clear()
        self.staff_admin = f.UserFactory(email="ops@platform.test", is_platform_staff=True)
        self.root = User.objects.create_superuser(email="root@platform.test", password=PASSWORD)
        self.starter = plan("starter", Decimal("29"), Decimal("290"))
        self.pro = plan("pro", Decimal("79"), Decimal("780"))
        self.org = f.OrganizationFactory(name="Glow Spa", slug="glow")
        subscribe(self.org, self.pro, Subscription.Status.ACTIVE)
        self.owner = f.MembershipFactory(organization=self.org, role=OrganizationRole.OWNER).user
        self.client.force_login(self.staff_admin)


PAGES = (
    ("saas-dashboard", ()),
    ("saas-organizations", ()),
    ("saas-organization-new", ()),
    ("saas-subscriptions", ()),
    ("saas-plans", ()),
    ("saas-plan-new", ()),
    ("saas-users", ()),
    ("saas-audit", ()),
)


class AccessTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_platform_staff_and_superusers_open_every_page(self):
        pages = [*PAGES, ("saas-organization", (self.org.pk,)), ("saas-user", (self.owner.pk,))]
        for user in (self.staff_admin, self.root):
            self.client.force_login(user)
            for name, args in pages:
                with self.subTest(user=user.email, page=name):
                    self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)

    def test_everyone_else_is_refused(self):
        customer = f.UserFactory()
        for user in (self.owner, customer):
            self.client.force_login(user)
            for name, args in PAGES:
                with self.subTest(user=user.email, page=name):
                    self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403)
        self.client.logout()
        response = self.client.get(reverse("saas-dashboard"))
        self.assertTrue(response["Location"].startswith(reverse("login")))

    def test_platform_staff_land_on_the_platform(self):
        self.assertRedirects(self.client.get(reverse("home")), reverse("saas-dashboard"))

    def test_a_deactivated_platform_account_has_no_access(self):
        self.staff_admin.is_active = False
        self.staff_admin.save()
        self.assertFalse(self.staff_admin.is_platform_user)


class OverviewTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        trial = f.OrganizationFactory()
        subscribe(trial, self.starter, Subscription.Status.TRIALING)
        yearly = f.OrganizationFactory()
        subscribe(yearly, self.pro, Subscription.Status.ACTIVE, Subscription.BillingCycle.YEARLY)
        suspended = f.OrganizationFactory(is_suspended=True)
        subscribe(suspended, self.starter, Subscription.Status.PAST_DUE)

    def test_the_numbers(self):
        overview = platform_overview(use_cache=False)
        self.assertEqual(overview["organizations"]["total"], 4)
        self.assertEqual(overview["organizations"]["suspended"], 1)
        self.assertEqual(overview["subscriptions"]["trialing"], 1)
        self.assertEqual(overview["subscriptions"]["paying"], 2)
        self.assertEqual(overview["mrr"], Decimal("79") + Decimal("65"))  # 780 / 12
        plans = {row["plan__name"]: row["count"] for row in overview["plans"]}
        self.assertEqual(plans, {"Pro": 2, "Starter": 2})

    def test_alerts_for_the_worker_and_failed_notifications(self):
        messages = [text for _, text in platform_overview(use_cache=False)["alerts"]]
        self.assertTrue(any("heartbeat" in text for text in messages))
        heartbeat()
        NotificationLog.objects.create(
            organization=self.org,
            recipient_email="x@example.test",
            status=NotificationLog.Status.FAILED,
        )
        messages = [text for _, text in platform_overview(use_cache=False)["alerts"]]
        self.assertFalse(any("heartbeat" in text for text in messages))
        self.assertTrue(any("1 notification failed" in text for text in messages))
        self.assertIsNotNone(cache.get(HEARTBEAT_KEY))

    def test_the_page(self):
        response = self.client.get(reverse("saas-dashboard"))
        self.assertContains(response, "144.00")  # MRR
        self.assertContains(response, "Subscriptions by plan")


class OrganizationTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_create_an_organization_with_a_trial_and_an_owner_invitation(self):
        response = self.client.post(
            reverse("saas-organization-new"),
            {
                "name": "Bright Clinic",
                "slug": "",
                "owner_email": "Owner@Bright.test",
                "plan": self.starter.pk,
                "timezone_name": "America/Toronto",
                "currency": "cad",
                "trial_days": 14,
            },
        )
        organization = Organization.objects.get(slug="bright-clinic")
        self.assertRedirects(response, reverse("saas-organization", args=[organization.pk]))
        self.assertEqual((organization.timezone, organization.currency), ("America/Toronto", "CAD"))
        self.assertTrue(
            Location.objects.filter(organization=organization, is_default=True).exists()
        )
        self.assertEqual(organization.subscription.status, Subscription.Status.TRIALING)
        self.assertGreater(organization.subscription.trial_end, timezone.now())
        invitation = OrganizationInvitation.objects.get(organization=organization)
        self.assertEqual((invitation.email, invitation.role), ("owner@bright.test", "owner"))
        entry = AuditLog.objects.get(action=AuditAction.ORGANIZATION_CREATED)
        self.assertEqual(
            (entry.user, entry.actor_type), (self.staff_admin, AuditLog.ActorType.PLATFORM_ADMIN)
        )

    def test_a_taken_address_is_refused(self):
        response = self.client.post(
            reverse("saas-organization-new"),
            {
                "name": "Glow",
                "slug": "glow",
                "owner_email": "a@b.test",
                "plan": self.starter.pk,
                "timezone_name": "UTC",
                "currency": "USD",
                "trial_days": 0,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "is taken", status_code=422)

    def test_suspend_needs_a_reason_and_blocks_members_until_reactivated(self):
        url = reverse("saas-organization-status", args=[self.org.pk, "suspend"])
        self.assertEqual(self.client.post(url, {"reason": ""}).status_code, 422)
        self.client.post(url, {"reason": "Unpaid invoices"})
        self.org.refresh_from_db()
        self.assertEqual(
            (self.org.is_suspended, self.org.suspension_reason), (True, "Unpaid invoices")
        )
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("app-dashboard")).status_code, 403)
        self.client.force_login(self.staff_admin)
        self.client.post(
            reverse("saas-organization-status", args=[self.org.pk, "reactivate"]),
            {"reason": "Paid"},
        )
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("app-dashboard")).status_code, 200)
        reasons = list(
            AuditLog.objects.filter(
                organization=self.org,
                action__in=(
                    AuditAction.ORGANIZATION_SUSPENDED,
                    AuditAction.ORGANIZATION_REACTIVATED,
                ),
            )
            .order_by("created_at")
            .values_list("metadata__reason", flat=True)
        )
        self.assertEqual(reasons, ["Unpaid invoices", "Paid"])
        self.assertEqual(
            self.client.post(
                reverse("saas-organization-status", args=[self.org.pk, "delete"]), {"reason": "x"}
            ).status_code,
            403,  # the owner, now: platform pages refuse them first
        )

    def test_the_detail_page_shows_counts_not_records(self):
        customer = f.CustomerFactory(organization=self.org, first_name="Zelda", last_name="Secret")
        booking = f.BookingFactory(organization=self.org, customer=customer)
        response = self.client.get(reverse("saas-organization", args=[self.org.pk]))
        self.assertContains(response, "Customers")
        self.assertNotContains(response, "Zelda")
        self.assertNotContains(response, booking.reference)

    def test_change_the_subscription(self):
        subscription = self.org.subscription
        response = self.client.post(
            reverse("saas-subscription-change", args=[self.org.pk]),
            {
                "plan": self.starter.pk,
                "status": Subscription.Status.PAST_DUE,
                "billing_cycle": Subscription.BillingCycle.MONTHLY,
                "trial_end": "",
                "current_period_end": "",
            },
        )
        self.assertRedirects(response, reverse("saas-organization", args=[self.org.pk]))
        subscription.refresh_from_db()
        self.assertEqual((subscription.plan, subscription.status), (self.starter, "past_due"))
        entry = AuditLog.objects.get(action=AuditAction.SUBSCRIPTION_CHANGED)
        self.assertEqual(entry.metadata["changes"]["plan"], {"from": "pro", "to": "starter"})


class PlanAndUserTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def plan_data(self, **overrides):
        return {
            "name": "Team",
            "slug": "team",
            "monthly_price": "149",
            "yearly_price": "1490",
            "maximum_staff": 20,
            "maximum_services": 100,
            "maximum_monthly_bookings": 5000,
            "maximum_locations": 5,
            "analytics_enabled": "on",
            "api_access_enabled": "on",
            "is_active": "on",
            **overrides,
        }

    def test_create_and_change_a_plan(self):
        self.assertRedirects(
            self.client.post(reverse("saas-plan-new"), self.plan_data()), reverse("saas-plans")
        )
        team = Plan.objects.get(slug="team")
        self.client.post(reverse("saas-plan", args=[team.pk]), self.plan_data(maximum_staff=25))
        team.refresh_from_db()
        self.assertEqual(team.maximum_staff, 25)
        actions = list(
            AuditLog.objects.filter(object_identifier=str(team.pk)).values_list("action", flat=True)
        )
        self.assertEqual(sorted(actions), [AuditAction.PLAN_CREATED, AuditAction.PLAN_UPDATED])

    def test_deactivate_and_reactivate_an_account(self):
        url = reverse("saas-user", args=[self.owner.pk])
        self.assertEqual(self.client.post(url, {"action": "deactivate"}).status_code, 422)
        self.client.post(url, {"action": "deactivate", "reason": "Fraud report"})
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        self.owner.set_password(PASSWORD)
        self.owner.save()
        self.assertFalse(self.client.login(email=self.owner.email, password=PASSWORD))
        self.client.force_login(self.staff_admin)
        self.client.post(url, {"action": "reactivate", "reason": "Cleared"})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

    def test_limits_on_account_changes(self):
        # Not yourself; not another platform account unless a superuser; platform access is
        # granted by superusers only.
        own = reverse("saas-user", args=[self.staff_admin.pk])
        self.assertEqual(
            self.client.post(own, {"action": "deactivate", "reason": "x"}).status_code, 403
        )
        colleague = f.UserFactory(is_platform_staff=True)
        url = reverse("saas-user", args=[colleague.pk])
        self.assertEqual(
            self.client.post(url, {"action": "deactivate", "reason": "x"}).status_code, 403
        )
        owner_url = reverse("saas-user", args=[self.owner.pk])
        self.assertEqual(self.client.post(owner_url, {"action": "grant_platform"}).status_code, 403)
        self.client.force_login(self.root)
        self.client.post(owner_url, {"action": "grant_platform"})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_platform_staff)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.PLATFORM_STAFF_GRANTED).exists())


class AuditLogTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def test_organization_entries_show_no_details(self):
        record_audit(
            AuditAction.CUSTOMER_UPDATED,
            organization=self.org,
            actor=self.owner,
            metadata={"changes": {"first_name": "SECRET-NAME"}, "reason": "SECRET-REASON"},
        )
        self.client.post(
            reverse("saas-organization-status", args=[self.org.pk, "suspend"]),
            {"reason": "Visible platform reason"},
        )
        response = self.client.get(reverse("saas-audit"))
        self.assertContains(response, AuditAction.CUSTOMER_UPDATED.value)
        self.assertNotContains(response, "SECRET")
        self.assertContains(response, "Visible platform reason")
        row = f'font-medium">{AuditAction.CUSTOMER_UPDATED.value}</td>'
        self.assertContains(response, row)
        response = self.client.get(reverse("saas-audit"), {"platform": "1"})
        self.assertNotContains(response, row)
