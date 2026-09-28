"""M1.1 (tenant resolution) and M1.2 (capability registry) tests."""

from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from bookings.models import Booking
from bookings.services import create_booking
from core.models import AuditLog
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.permissions import (
    PERMISSIONS_DOC_PATH,
    ROLE_CAPABILITIES,
    Capability,
    can_grant_role,
    capabilities_for,
    render_permissions_markdown,
)
from organizations.tenancy import (
    SESSION_KEY,
    TenantSuspended,
    reactivate_organization,
    resolve_tenant,
    suspend_organization,
)
from scheduling.models import WeeklyAvailability
from services.models import Service
from staff.models import StaffProfile

User = get_user_model()


def make_user(email, **extra):
    return User.objects.create_user(email=email, password="Password12345!", **extra)


def join(user, organization, role, **extra):
    return OrganizationMembership.objects.create(
        user=user, organization=organization, role=role, **extra
    )


class TwoTenantMixin:
    def setUp(self):
        self.org_a = Organization.objects.create(name="Org A", slug="org-a")
        self.org_b = Organization.objects.create(name="Org B", slug="org-b")
        self.users = {}
        for role in OrganizationRole.values:
            user = make_user(f"{role}@a.test")
            join(user, self.org_a, role)
            self.users[role] = user
        self.owner_b = make_user("owner@b.test")
        join(self.owner_b, self.org_b, OrganizationRole.OWNER)

    def as_role(self, role):
        self.client.force_authenticate(self.users[role])
        return self.users[role]


# --------------------------------------------------------------------------- M1.1 resolution


class ResolveTenantTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.org_a = Organization.objects.create(name="Org A", slug="org-a")
        self.org_b = Organization.objects.create(name="Org B", slug="org-b")
        self.org_c = Organization.objects.create(name="Org C", slug="org-c")
        self.user = make_user("multi@test.test")
        join(self.user, self.org_a, OrganizationRole.MANAGER)
        join(self.user, self.org_b, OrganizationRole.STAFF)

    def request(self, slug=None, session=None):
        headers = {"HTTP_X_ORGANIZATION_SLUG": slug} if slug else {}
        request = self.factory.get("/", **headers)
        request.user = self.user
        request.session = session or {}
        return request

    def test_default_is_oldest_membership(self):
        self.assertEqual(resolve_tenant(self.request()).organization, self.org_a)

    def test_slug_selects_among_own_memberships(self):
        context = resolve_tenant(self.request("org-b"))
        self.assertEqual(context.organization, self.org_b)
        self.assertEqual(context.role, OrganizationRole.STAFF)

    def test_slug_of_non_member_org_gives_no_context(self):
        self.assertIsNone(resolve_tenant(self.request("org-c")))

    def test_unknown_slug_does_not_fall_back_to_default(self):
        self.assertIsNone(resolve_tenant(self.request("does-not-exist")))

    def test_session_active_organization_is_used(self):
        request = self.request(session={SESSION_KEY: str(self.org_b.pk)})
        self.assertEqual(resolve_tenant(request).organization, self.org_b)

    def test_inactive_membership_gives_no_context(self):
        OrganizationMembership.objects.filter(user=self.user).update(is_active=False)
        self.assertIsNone(resolve_tenant(self.request()))

    def test_inactive_organization_gives_no_context(self):
        Organization.objects.filter(pk=self.org_a.pk).update(is_active=False)
        self.assertEqual(resolve_tenant(self.request()).organization, self.org_b)
        self.assertIsNone(resolve_tenant(self.request("org-a")))

    def test_explicitly_selected_suspended_org_raises(self):
        suspend_organization(organization=self.org_a, reason="unpaid")
        with self.assertRaises(TenantSuspended):
            resolve_tenant(self.request("org-a"))

    def test_default_skips_suspended_org(self):
        suspend_organization(organization=self.org_a, reason="unpaid")
        self.assertEqual(resolve_tenant(self.request()).organization, self.org_b)

    def test_superuser_without_membership_has_no_context(self):
        admin = User.objects.create_superuser(email="root@test.test", password="Root12345!!")
        request = self.factory.get("/", HTTP_X_ORGANIZATION_SLUG="org-a")
        request.user = admin
        request.session = {}
        self.assertIsNone(resolve_tenant(request))

    def test_suspend_and_reactivate_are_audited(self):
        suspend_organization(organization=self.org_a, reason="unpaid", actor=self.user)
        self.org_a.refresh_from_db()
        self.assertTrue(self.org_a.is_suspended)
        self.assertEqual(self.org_a.suspension_reason, "unpaid")
        self.assertIsNotNone(self.org_a.suspended_at)
        reactivate_organization(organization=self.org_a, actor=self.user)
        self.org_a.refresh_from_db()
        self.assertFalse(self.org_a.is_suspended)
        actions = set(AuditLog.objects.values_list("action", flat=True))
        self.assertTrue({"organization.suspended", "organization.reactivated"} <= actions)


class TenantEndpointTests(TwoTenantMixin, APITestCase):
    def test_non_member_slug_is_denied(self):
        self.as_role(OrganizationRole.OWNER)
        response = self.client.get("/api/v1/services/", HTTP_X_ORGANIZATION_SLUG="org-b")
        self.assertEqual(response.status_code, 403)

    def test_suspended_org_is_denied_with_clear_code(self):
        suspend_organization(organization=self.org_a, reason="unpaid")
        self.as_role(OrganizationRole.OWNER)
        response = self.client.get("/api/v1/services/", HTTP_X_ORGANIZATION_SLUG="org-a")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "This organization is suspended.")

    def test_superuser_gets_no_tenant_data(self):
        Service.objects.create(
            organization=self.org_b, name="S", slug="s", price=1, duration_minutes=30
        )
        admin = User.objects.create_superuser(email="root@test.test", password="Root12345!!")
        self.client.force_authenticate(admin)
        response = self.client.get("/api/v1/services/", HTTP_X_ORGANIZATION_SLUG="org-b")
        self.assertEqual(response.status_code, 403)


# --------------------------------------------------------------------------- M1.2 capabilities


class CapabilityRegistryTests(TestCase):
    def test_every_role_has_a_capability_set(self):
        self.assertEqual(set(ROLE_CAPABILITIES), set(OrganizationRole.values))

    def test_owner_has_everything(self):
        self.assertEqual(ROLE_CAPABILITIES[OrganizationRole.OWNER], frozenset(Capability))

    def test_key_defaults_match_the_agreed_matrix(self):
        expectations = {
            OrganizationRole.MANAGER: (
                {"staff.manage", "reports.view", "customers.manage"},
                {"billing.manage", "organization.manage"},
            ),
            OrganizationRole.RECEPTIONIST: (
                {"appointments.manage", "appointments.view_all", "customers.manage"},
                {"settings.manage", "billing.view", "reports.view", "staff.manage"},
            ),
            OrganizationRole.STAFF: (
                {"appointments.manage", "services.view"},
                {"appointments.view_all", "customers.view", "reports.view"},
            ),
            OrganizationRole.CUSTOMER: ({"organization.view"}, {"customers.view"}),
        }
        for role, (allowed, denied) in expectations.items():
            capabilities = {c.value for c in ROLE_CAPABILITIES[role]}
            with self.subTest(role=role):
                self.assertTrue(allowed <= capabilities)
                self.assertFalse(denied & capabilities)

    def test_overrides_grant_and_revoke(self):
        capabilities = capabilities_for(
            OrganizationRole.RECEPTIONIST, granted=["reports.view"], revoked=["customers.manage"]
        )
        self.assertIn(Capability.REPORTS_VIEW, capabilities)
        self.assertNotIn(Capability.CUSTOMERS_MANAGE, capabilities)

    def test_membership_rejects_unknown_capability_codes(self):
        org = Organization.objects.create(name="O", slug="o")
        membership = OrganizationMembership(
            organization=org,
            user=make_user("x@test.test"),
            role=OrganizationRole.STAFF,
            granted_permissions=["reports.view", "make.coffee"],
        )
        with self.assertRaises(ValidationError):
            membership.clean()

    def test_role_ceiling(self):
        self.assertTrue(can_grant_role(OrganizationRole.MANAGER, OrganizationRole.RECEPTIONIST))
        self.assertTrue(can_grant_role(OrganizationRole.MANAGER, OrganizationRole.MANAGER))
        self.assertFalse(can_grant_role(OrganizationRole.MANAGER, OrganizationRole.OWNER))
        self.assertFalse(can_grant_role(OrganizationRole.STAFF, OrganizationRole.MANAGER))

    def test_permissions_doc_matches_registry(self):
        path = Path(settings.BASE_DIR) / PERMISSIONS_DOC_PATH
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            render_permissions_markdown(),
            "docs/PERMISSIONS.md is stale: run `python manage.py generate_permissions_doc`",
        )


class RoleEndpointMatrixTests(TwoTenantMixin, APITestCase):
    """Server-side enforcement of the matrix on real endpoints (status codes per role)."""

    CASES = [
        # (method, path, {role: expected_status})
        (
            "get",
            "/api/v1/services/",
            {"owner": 200, "receptionist": 200, "staff": 200, "customer": 403},
        ),
        (
            "post",
            "/api/v1/service-categories/",
            {"manager": 201, "receptionist": 403, "staff": 403},
        ),
        (
            "get",
            "/api/v1/customers/",
            {"manager": 200, "receptionist": 200, "staff": 403, "customer": 403},
        ),
        ("get", "/api/v1/waitlist/", {"receptionist": 200, "staff": 403, "customer": 403}),
        (
            "get",
            "/api/dashboard/summary/",
            {"owner": 200, "manager": 200, "receptionist": 403, "staff": 403},
        ),
        ("get", "/api/organizations/memberships/", {"manager": 200, "receptionist": 403}),
        ("get", "/api/organizations/current/", {"customer": 200, "staff": 200}),
        (
            "patch",
            "/api/organizations/current/",
            {"owner": 200, "manager": 403, "receptionist": 403},
        ),
        ("get", "/api/v1/staff/", {"receptionist": 200, "customer": 403}),
        ("post", "/api/v1/staff/", {"receptionist": 403, "staff": 403}),
    ]

    def payload(self, method, path):
        if path == "/api/v1/service-categories/":
            return {"name": "Massage", "slug": "massage"}
        if method == "patch":
            return {"description": "updated"}
        return {}

    def test_matrix(self):
        for method, path, expected in self.CASES:
            for role, status_code in expected.items():
                with self.subTest(method=method, path=path, role=role):
                    self.as_role(role)
                    call = getattr(self.client, method)
                    response = call(path, self.payload(method, path), format="json")
                    self.assertEqual(response.status_code, status_code, response.content[:200])
                    if method == "post" and status_code == 201:
                        self.client.delete(f"{path}{response.json()['id']}/")

    def test_granted_capability_opens_endpoint(self):
        OrganizationMembership.objects.filter(user=self.users["receptionist"]).update(
            granted_permissions=["reports.view"]
        )
        self.as_role(OrganizationRole.RECEPTIONIST)
        self.assertEqual(self.client.get("/api/dashboard/summary/").status_code, 200)

    def test_revoked_capability_closes_endpoint(self):
        OrganizationMembership.objects.filter(user=self.users["manager"]).update(
            revoked_permissions=["customers.view", "customers.manage"]
        )
        self.as_role(OrganizationRole.MANAGER)
        self.assertEqual(self.client.get("/api/v1/customers/").status_code, 403)

    def test_created_category_belongs_to_callers_org(self):
        self.as_role(OrganizationRole.MANAGER)
        response = self.client.post(
            "/api/v1/service-categories/", {"name": "Spa", "slug": "spa"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["organization"], str(self.org_a.pk))

    def test_membership_list_exposes_effective_capabilities(self):
        self.as_role(OrganizationRole.MANAGER)
        rows = self.client.get("/api/organizations/memberships/").json()["results"]
        receptionist = next(r for r in rows if r["role"] == "receptionist")
        self.assertIn("appointments.manage", receptionist["capabilities"])
        self.assertNotIn("reports.view", receptionist["capabilities"])


class InvitationCeilingTests(TwoTenantMixin, APITestCase):
    def invite(self, role):
        return self.client.post(
            "/api/organizations/invitations/", {"email": "new@a.test", "role": role}, format="json"
        )

    def test_manager_cannot_invite_owner(self):
        self.as_role(OrganizationRole.MANAGER)
        response = self.invite(OrganizationRole.OWNER)
        self.assertEqual(response.status_code, 400)
        self.assertIn("role", response.json())

    def test_manager_can_invite_receptionist_without_expiry(self):
        self.as_role(OrganizationRole.MANAGER)
        response = self.invite(OrganizationRole.RECEPTIONIST)
        self.assertEqual(response.status_code, 201, response.content)

    def test_owner_can_invite_owner(self):
        self.as_role(OrganizationRole.OWNER)
        self.assertEqual(self.invite(OrganizationRole.OWNER).status_code, 201)

    def test_receptionist_cannot_invite(self):
        self.as_role(OrganizationRole.RECEPTIONIST)
        self.assertEqual(self.invite(OrganizationRole.CUSTOMER).status_code, 403)


# --------------------------------------------------------------------------- booking visibility


class BookingVisibilityTests(TwoTenantMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.provider = StaffProfile.objects.create(
            user=self.users["staff"], organization=self.org_a
        )
        other_user = make_user("other-provider@a.test")
        join(other_user, self.org_a, OrganizationRole.STAFF)
        self.other_provider = StaffProfile.objects.create(user=other_user, organization=self.org_a)
        self.service = Service.objects.create(
            organization=self.org_a, name="Massage", slug="massage", price=50, duration_minutes=60
        )
        start = timezone.now() + timedelta(days=2)
        self.own = self.book(self.provider, start, "victim@example.test")
        self.others = self.book(self.other_provider, start, "someone@example.test")

    def book(self, staff, start, email):
        return create_booking(
            organization=self.org_a,
            service=self.service,
            staff_profile=staff,
            customer_name="C",
            customer_email=email,
            customer_phone="",
            start_datetime=start,
        )

    def ids(self, **headers):
        response = self.client.get("/api/v1/bookings/", **headers)
        self.assertEqual(response.status_code, 200, response.content)
        return {row["id"] for row in response.json()["results"]}

    def test_receptionist_sees_all_org_bookings(self):
        self.as_role(OrganizationRole.RECEPTIONIST)
        self.assertEqual(self.ids(), {str(self.own.id), str(self.others.id)})

    def test_staff_sees_only_own_bookings(self):
        self.as_role(OrganizationRole.STAFF)
        self.assertEqual(self.ids(), {str(self.own.id)})

    def test_other_tenant_owner_sees_nothing(self):
        self.client.force_authenticate(self.owner_b)
        self.assertEqual(self.ids(), set())
        self.assertEqual(self.ids(HTTP_X_ORGANIZATION_SLUG="org-a"), set())

    def test_unverified_email_does_not_expose_bookings(self):
        impostor = make_user("victim@example.test")
        self.client.force_authenticate(impostor)
        self.assertEqual(self.ids(), set())

    def test_verified_email_sees_own_bookings(self):
        customer = make_user("victim@example.test", email_verified=True)
        self.client.force_authenticate(customer)
        self.assertEqual(self.ids(), {str(self.own.id)})

    def test_anonymous_cannot_list(self):
        self.assertEqual(self.client.get("/api/v1/bookings/").status_code, 401)

    def test_team_booking_does_not_link_customer_to_team_member(self):
        receptionist = self.as_role(OrganizationRole.RECEPTIONIST)
        start = (timezone.now() + timedelta(days=3)).isoformat()
        response = self.client.post(
            "/api/v1/bookings/",
            {
                "service": str(self.service.id),
                "staff": str(self.provider.id),
                "start_datetime": start,
                "customer_name": "Walk In",
                "customer_email": "walkin@example.test",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        booking = Booking.objects.get(id=response.json()["id"])
        self.assertEqual(booking.organization, self.org_a)
        self.assertIsNone(booking.customer.user)
        self.assertNotEqual(booking.customer.user, receptionist)

    def test_booking_conflict_is_400_not_500(self):
        self.as_role(OrganizationRole.RECEPTIONIST)
        response = self.client.post(
            "/api/v1/bookings/",
            {
                "service": str(self.service.id),
                "staff": str(self.provider.id),
                "start_datetime": self.own.start_datetime.isoformat(),
                "customer_name": "Late",
                "customer_email": "late@example.test",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)


# --------------------------------------------------------------------------- public slots


class PublicSlotTests(APITestCase):
    URL = "/api/v1/availability/slots/available-slots/"

    def setUp(self):
        self.org = Organization.objects.create(name="Spa", slug="spa")
        self.staff = StaffProfile.objects.create(
            user=make_user("p@spa.test"), organization=self.org
        )
        self.service = Service.objects.create(
            organization=self.org, name="Facial", slug="facial", price=80, duration_minutes=60
        )
        self.day = date.today() + timedelta(days=7)
        WeeklyAvailability.objects.create(
            organization=self.org,
            staff=self.staff,
            day_of_week=self.day.weekday(),
            start_time="09:00",
            end_time="12:00",
        )

    def get(self, **overrides):
        params = {
            "organization": "spa",
            "service": str(self.service.id),
            "staff": str(self.staff.id),
            "date": self.day.isoformat(),
            **overrides,
        }
        return self.client.get(self.URL, params)

    def test_anonymous_gets_slots_for_public_org(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["slots"])

    def test_non_public_service_is_404(self):
        Service.objects.filter(pk=self.service.pk).update(is_public=False)
        self.assertEqual(self.get().status_code, 404)

    def test_suspended_org_is_404(self):
        suspend_organization(organization=self.org, reason="x")
        self.assertEqual(self.get().status_code, 404)

    def test_malformed_ids_are_400_not_500(self):
        self.assertEqual(self.get(service="not-a-uuid").status_code, 400)
        self.assertEqual(self.get(date="31-12-2026").status_code, 400)
