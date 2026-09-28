"""M1.5 tenant isolation suite.

Proves that a member of organization A (here its *owner*, the most privileged tenant role)
cannot retrieve, update, delete, act on, list, search, filter or infer organization B's data
through any API endpoint.

The suite is driven by the API router. Every registered resource must have an entry in
``B_OBJECT_FACTORIES`` (or be explicitly public), so a newly added endpoint fails
``test_every_api_resource_is_covered`` until it is added here.
"""

import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from api.urls import router
from bookings.models import Customer
from core.audit import snapshot
from organizations.models import OrganizationRole
from services.models import Service
from tests import factories as f

SECRET = "zz-tenant-b-secret-zz"
NOT_ALLOWED = {403, 404, 405}

# Public by design: anonymous slot search on organizations with a public booking page.
PUBLIC_RESOURCES = {"availability-slots"}
# Open to non-members for their *own* customer rows (a customer listing their appointments).
OWN_CUSTOMER_RESOURCES = {"booking"}

# One object per API resource. ``mark`` goes into searchable text: organization B's objects get
# SECRET, organization A's own objects get a harmless marker.
B_OBJECT_FACTORIES = {
    "service": lambda org, mark: f.ServiceFactory(organization=org, name=f"{mark} massage"),
    "service-category": lambda org, mark: f.ServiceCategoryFactory(organization=org, name=mark),
    "staff": lambda org, mark: f.StaffProfileFactory(organization=org, job_title=mark),
    "availability-weekly": lambda org, mark: f.WeeklyAvailabilityFactory(organization=org),
    "availability-exceptions": lambda org, mark: f.AvailabilityExceptionFactory(
        organization=org, reason=mark
    ),
    "availability-time-off": lambda org, mark: f.TimeOffFactory(organization=org, reason=mark),
    "availability-holidays": lambda org, mark: f.OrganizationHolidayFactory(
        organization=org, name=mark
    ),
    "booking": lambda org, mark: f.BookingFactory(organization=org, customer_name=mark),
    "customer": lambda org, mark: f.CustomerFactory(organization=org, first_name=mark),
    "tag": lambda org, mark: f.CustomerTagFactory(organization=org, tag__name=mark).tag,
    "customer-note": lambda org, mark: f.CustomerNoteFactory(organization=org, content=mark),
    "waitlist": lambda org, mark: f.WaitlistEntryFactory(organization=org, customer_name=mark),
    "audit-log": lambda org, mark: f.AuditLogFactory(organization=org, metadata={"note": mark}),
}


def api_resources():
    for prefix, viewset, basename in router.registry:
        if basename not in PUBLIC_RESOURCES:
            yield prefix, viewset, basename


class TenantIsolationSuite(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org_a = f.OrganizationFactory(slug="org-a")
        cls.org_b = f.OrganizationFactory(slug="org-b")
        cls.attacker = f.MembershipFactory(organization=cls.org_a, role=OrganizationRole.OWNER).user
        cls.b_member = f.MembershipFactory(organization=cls.org_b, role=OrganizationRole.OWNER).user
        # A has its own data too, so lists are non-empty and scoping is really exercised.
        for build in B_OBJECT_FACTORIES.values():
            build(cls.org_a, "tenant-a-own")
        cls.b_objects = {
            name: build(cls.org_b, SECRET) for name, build in B_OBJECT_FACTORIES.items()
        }

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.attacker)

    # -- helpers -----------------------------------------------------------------------------

    def list_ids(self, prefix, **params):
        response = self.client.get(f"/api/v1/{prefix}/", params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        rows = body["results"] if isinstance(body, dict) else body
        return {str(row["id"]) for row in rows}, response.content.decode()

    @staticmethod
    def db_state(obj):
        """Snapshot of the row as stored (factory instances may hold un-normalized values)."""
        return snapshot(type(obj).objects.get(pk=obj.pk))

    def assert_unchanged(self, obj, before):
        self.assertEqual(self.db_state(obj), before)

    # -- coverage ----------------------------------------------------------------------------

    def test_every_api_resource_is_covered(self):
        registered = {basename for _, _, basename in api_resources()}
        self.assertEqual(
            registered - set(B_OBJECT_FACTORIES),
            set(),
            "New API resource without tenant-isolation coverage: add it to B_OBJECT_FACTORIES",
        )

    # -- reads -------------------------------------------------------------------------------

    def test_list_and_search_never_show_other_tenant(self):
        for prefix, _, basename in api_resources():
            b_id = str(self.b_objects[basename].pk)
            with self.subTest(resource=basename, check="list"):
                ids, body = self.list_ids(prefix)
                self.assertTrue(ids, "own data should be listed")
                self.assertNotIn(b_id, ids)
                self.assertNotIn(SECRET, body)
            with self.subTest(resource=basename, check="search"):
                ids, body = self.list_ids(prefix, search=SECRET)
                self.assertNotIn(b_id, ids)
                self.assertNotIn(SECRET, body)

    def test_retrieve_other_tenant_object_is_404(self):
        for prefix, _, basename in api_resources():
            with self.subTest(resource=basename):
                response = self.client.get(f"/api/v1/{prefix}/{self.b_objects[basename].pk}/")
                self.assertEqual(response.status_code, 404)

    def test_selecting_other_tenant_by_header_is_denied(self):
        for prefix, _, basename in api_resources():
            with self.subTest(resource=basename):
                response = self.client.get(
                    f"/api/v1/{prefix}/", HTTP_X_ORGANIZATION_SLUG=self.org_b.slug
                )
                if basename in OWN_CUSTOMER_RESOURCES:
                    # Not a member of B: only the caller's *own* customer rows at B's public
                    # booking page. The attacker has none, so nothing of B may appear.
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["count"], 0)
                    self.assertNotIn(SECRET, response.content.decode())
                else:
                    self.assertEqual(response.status_code, 403)

    # -- writes ------------------------------------------------------------------------------

    def test_update_and_delete_other_tenant_object_fail(self):
        for prefix, _, basename in api_resources():
            obj = self.b_objects[basename]
            before = self.db_state(obj)
            url = f"/api/v1/{prefix}/{obj.pk}/"
            for method in ("put", "patch", "delete"):
                with self.subTest(resource=basename, method=method):
                    response = getattr(self.client, method)(url, {"name": "pwned"}, format="json")
                    self.assertIn(response.status_code, NOT_ALLOWED)
            self.assert_unchanged(obj, before)

    def test_custom_actions_on_other_tenant_object_fail(self):
        for prefix, viewset, basename in api_resources():
            obj = self.b_objects[basename]
            before = self.db_state(obj)
            for extra in viewset.get_extra_actions():
                if not extra.detail:
                    continue
                url = f"/api/v1/{prefix}/{obj.pk}/{extra.url_path}/"
                with self.subTest(resource=basename, action=extra.url_path):
                    response = self.client.post(url, {"status": "completed"}, format="json")
                    self.assertIn(response.status_code, NOT_ALLOWED)
            self.assert_unchanged(obj, before)

    def test_other_tenant_objects_survive_the_attack(self):
        self.test_update_and_delete_other_tenant_object_fail()
        for basename, obj in self.b_objects.items():
            with self.subTest(resource=basename):
                self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists())

    # -- inference ---------------------------------------------------------------------------

    def test_filters_do_not_reveal_other_tenant_ids(self):
        """Filtering by B's id must look exactly like filtering by an id that does not exist."""
        cases = [
            ("bookings", "service", self.b_objects["booking"].service_id),
            ("bookings", "staff", self.b_objects["booking"].staff_id),
            ("services", "category", self.b_objects["service"].category_id),
            ("customers", "tag", self.b_objects["tag"].pk),
            ("customer-notes", "customer", self.b_objects["customer"].pk),
        ]
        for prefix, param, b_id in cases:
            missing = uuid.uuid4()
            with self.subTest(prefix=prefix, param=param):
                with_b = self.client.get(f"/api/v1/{prefix}/", {param: str(b_id)})
                with_missing = self.client.get(f"/api/v1/{prefix}/", {param: str(missing)})
                self.assertEqual(with_b.status_code, with_missing.status_code)
                self.assertEqual(
                    with_b.content.decode().replace(str(b_id), "ID"),
                    with_missing.content.decode().replace(str(missing), "ID"),
                )

    def test_writes_referencing_other_tenant_ids_look_like_missing_ids(self):
        b_staff = self.b_objects["staff"]
        payload = {"day_of_week": 1, "start_time": "09:00", "end_time": "10:00"}
        with_b = self.client.post(
            "/api/v1/availability/weekly/", {**payload, "staff": str(b_staff.pk)}, format="json"
        )
        missing = uuid.uuid4()
        with_missing = self.client.post(
            "/api/v1/availability/weekly/", {**payload, "staff": str(missing)}, format="json"
        )
        self.assertEqual(with_b.status_code, 400)
        self.assertEqual(
            with_b.json()["staff"][0].replace(str(b_staff.pk), "ID"),
            with_missing.json()["staff"][0].replace(str(missing), "ID"),
        )

    def test_search_never_returns_other_tenant_data(self):
        # B's booking reference and customer name are searchable text too.
        b_booking = self.b_objects["booking"]
        for query in (SECRET, b_booking.reference):
            with self.subTest(endpoint="global", query=query):
                response = self.client.get("/api/v1/search/", {"q": query})
                self.assertEqual(response.status_code, 200)
                results = response.json()["results"]
                self.assertTrue(all(rows == [] for rows in results.values()), results)
            with self.subTest(endpoint="customers", query=query):
                response = self.client.get("/api/v1/customers/search/", {"q": query})
                self.assertEqual(response.json(), [])
        for url in ("/api/v1/search/", "/api/v1/customers/search/"):
            with self.subTest(url=url, check="header"):
                response = self.client.get(
                    url, {"q": SECRET}, HTTP_X_ORGANIZATION_SLUG=self.org_b.slug
                )
                self.assertEqual(response.status_code, 403)

    def test_other_tenant_customer_detail_actions_are_404(self):
        b_customer = self.b_objects["customer"]
        for path in ("timeline", "notes", "appointments"):
            with self.subTest(action=path):
                response = self.client.get(f"/api/v1/customers/{b_customer.pk}/{path}/")
                self.assertEqual(response.status_code, 404)
        own = f.CustomerFactory(organization=self.org_a)
        response = self.client.post(
            f"/api/v1/customers/{own.pk}/merge/", {"duplicate": str(b_customer.pk)}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Customer.objects.filter(pk=b_customer.pk).exists())

    def test_other_tenant_customer_timeline_is_404(self):
        b_customer = self.b_objects["customer"]
        f.CustomerActivityFactory(
            organization=self.org_b, customer=b_customer, metadata={"note": SECRET}
        )
        response = self.client.get(f"/api/v1/customers/{b_customer.pk}/timeline/")
        self.assertEqual(response.status_code, 404)
        own = f.CustomerFactory(organization=self.org_a)
        body = self.client.get(f"/api/v1/customers/{own.pk}/timeline/").content.decode()
        self.assertNotIn(SECRET, body)

    def test_notes_cannot_be_written_for_other_tenant_customers(self):
        b_customer = self.b_objects["customer"]
        response = self.client.post(
            "/api/v1/customer-notes/",
            {"customer": str(b_customer.pk), "content": "pwned"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(b_customer.customer_notes.filter(content="pwned").exists())

    def test_tagging_with_other_tenant_tag_looks_like_a_missing_tag(self):
        customer = f.CustomerFactory(organization=self.org_a)
        b_tag, missing = self.b_objects["tag"], uuid.uuid4()
        url = f"/api/v1/customers/{customer.pk}/"
        with_b = self.client.patch(url, {"tag_ids": [str(b_tag.pk)]}, format="json")
        with_missing = self.client.patch(url, {"tag_ids": [str(missing)]}, format="json")
        self.assertEqual(with_b.status_code, 400)
        self.assertEqual(
            str(with_b.json()).replace(str(b_tag.pk), "ID"),
            str(with_missing.json()).replace(str(missing), "ID"),
        )
        self.assertFalse(customer.tag_set.exists())

    # -- non-router tenant endpoints ---------------------------------------------------------

    def test_membership_list_is_scoped(self):
        rows = self.client.get("/api/organizations/memberships/").json()["results"]
        emails = {row["user_email"] for row in rows}
        self.assertIn(self.attacker.email, emails)
        self.assertNotIn(self.b_member.email, emails)

    def test_current_organization_is_own(self):
        body = self.client.get("/api/organizations/current/").json()
        self.assertEqual(body["id"], str(self.org_a.pk))
        response = self.client.get(
            "/api/organizations/current/", HTTP_X_ORGANIZATION_SLUG=self.org_b.slug
        )
        self.assertEqual(response.status_code, 403)

    def test_dashboard_counts_only_own_tenant(self):
        body = self.client.get("/api/dashboard/summary/").json()
        self.assertEqual(
            body["active_services"],
            Service.objects.filter(organization=self.org_a, is_archived=False).count(),
        )
        self.assertEqual(
            body["active_customers"], Customer.objects.filter(organization=self.org_a).count()
        )
        self.assertEqual(body["confirmed_bookings"], 1)
        response = self.client.get(
            "/api/dashboard/summary/", HTTP_X_ORGANIZATION_SLUG=self.org_b.slug
        )
        self.assertEqual(response.status_code, 403)
