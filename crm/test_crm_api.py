"""M2.4: CRM API, provider access and search."""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking, Customer
from core.models import AuditLog
from crm.models import CustomerNote
from crm.selectors import search_filter
from crm.services import create_note
from organizations.models import OrganizationRole
from tests import factories as f

VISIBLE = CustomerNote.Visibility.CUSTOMER_VISIBLE


class Base(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.client = APIClient()

    def member(self, role):
        return f.MembershipFactory(organization=self.organization, role=role).user

    def as_user(self, user):
        self.client.force_authenticate(user)
        return self.client

    def customer(self, **kwargs):
        return f.CustomerFactory(organization=self.organization, **kwargs)


class PhoneSearchTests(Base):
    def test_phone_matches_in_any_format(self):
        ada = self.customer(phone="+1 (416) 555-0101")
        bob = self.customer(phone="416.555.0199", secondary_phone="647-555-0101")
        self.customer(phone="+44 20 7946 0958")
        cases = {
            "(416) 555-0101": {ada},
            "416-555-0101": {ada},
            "+14165550101": {ada},
            "1 416 555 0101": {ada},
            "5550101": {ada, bob},  # bob's secondary phone ends the same way
            "647 555 0101": {bob},
            "4165550199": {bob},
            "+1 416 555 0199": {bob},  # stored without the country code
        }
        customers = Customer.objects.filter(organization=self.organization)
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(set(search_filter(customers, query)), expected)

    def test_words_match_names_and_email(self):
        ada = self.customer(first_name="Ada", last_name="Lovelace", email="ada@math.test")
        self.customer(first_name="Ada", last_name="Byron", email="byron@poet.test")
        customers = Customer.objects.filter(organization=self.organization)
        self.assertEqual(list(search_filter(customers, "ada love")), [ada])
        self.assertEqual(list(search_filter(customers, "MATH.TEST")), [ada])

    def test_phone_search_stays_in_sync(self):
        customer = self.customer(phone="+14165550101")
        customer.phone = "905-555-0100"
        customer.save(update_fields=["phone"])
        customer.refresh_from_db()
        self.assertEqual(customer.phone_search, "9055550100")


class CustomerListFilterTests(Base):
    def setUp(self):
        super().setUp()
        self.as_user(self.member(OrganizationRole.MANAGER))
        self.maya = f.StaffProfileFactory(organization=self.organization)
        self.dan = f.StaffProfileFactory(organization=self.organization)

    def ids(self, **params):
        rows = self.client.get("/api/v1/customers/", params).json()["results"]
        return {row["id"] for row in rows}

    def visit(self, customer, days_ago, staff=None, status=Booking.Status.COMPLETED):
        return f.BookingFactory(
            organization=self.organization,
            customer=customer,
            staff=staff or self.maya,
            status=status,
            start_datetime=timezone.now() - timedelta(days=days_ago),
        )

    def test_staff_filters(self):
        assigned = self.customer(assigned_staff=self.maya)
        prefers = self.customer(preferred_staff=self.maya)
        booked = self.customer()
        self.visit(booked, 3)
        other = self.customer(assigned_staff=self.dan)
        self.assertEqual(self.ids(assigned_staff=self.maya.pk), {str(assigned.pk)})
        self.assertEqual(self.ids(preferred_staff=self.maya.pk), {str(prefers.pk)})
        self.assertEqual(
            self.ids(staff=self.maya.pk), {str(assigned.pk), str(prefers.pk), str(booked.pk)}
        )
        self.assertNotIn(str(other.pk), self.ids(staff=self.maya.pk))

    def test_last_visit_filters_and_ordering(self):
        recent, lapsed, never = self.customer(), self.customer(), self.customer()
        self.visit(recent, 10)
        self.visit(lapsed, 400)
        self.visit(lapsed, 5, status=Booking.Status.CANCELLED)  # not a visit
        cutoff = (timezone.now() - timedelta(days=180)).date()
        self.assertEqual(self.ids(last_visit_before=cutoff), {str(lapsed.pk)})
        self.assertEqual(self.ids(last_visit_after=cutoff), {str(recent.pk)})
        self.assertEqual(self.ids(never_visited=True), {str(never.pk)})

        rows = self.client.get("/api/v1/customers/", {"ordering": "-last_visit"}).json()
        visited = [row["id"] for row in rows["results"] if row["last_visit"]]
        self.assertEqual(visited, [str(recent.pk), str(lapsed.pk)])

    def test_search_param_uses_phone_normalization(self):
        ada = self.customer(phone="+14165550101")
        self.assertEqual(self.ids(search="(416) 555-0101"), {str(ada.pk)})

    def test_list_query_count_does_not_grow_with_rows(self):
        tag = f.TagFactory(organization=self.organization)

        def count_queries():
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get("/api/v1/customers/")
            self.assertEqual(response.status_code, 200)
            return len(queries)

        for _ in range(3):
            f.CustomerTagFactory(organization=self.organization, tag=tag)
        few = count_queries()
        for _ in range(20):
            f.CustomerTagFactory(organization=self.organization, tag=tag)
        self.assertEqual(count_queries(), few)
        self.assertLessEqual(few, 12)


class ProviderAccessTests(Base):
    """Providers (staff role) see only their own customers, read-only."""

    def setUp(self):
        super().setUp()
        self.provider = self.member(OrganizationRole.STAFF)
        self.profile = f.StaffProfileFactory(organization=self.organization, user=self.provider)
        self.manager = self.member(OrganizationRole.MANAGER)
        self.assigned = self.customer(assigned_staff=self.profile)
        self.treated = self.customer()
        self.booking = f.BookingFactory(
            organization=self.organization, customer=self.treated, staff=self.profile
        )
        self.stranger = self.customer()
        self.other_booking = f.BookingFactory(organization=self.organization, customer=self.treated)

    def test_sees_only_assigned_and_treated_customers(self):
        client = self.as_user(self.provider)
        rows = client.get("/api/v1/customers/").json()["results"]
        self.assertEqual({r["id"] for r in rows}, {str(self.assigned.pk), str(self.treated.pk)})
        self.assertEqual(client.get(f"/api/v1/customers/{self.stranger.pk}/").status_code, 404)
        self.assertEqual(client.get("/api/v1/customers/search/", {"q": "zz"}).status_code, 200)

    def test_is_read_only(self):
        client = self.as_user(self.provider)
        url = f"/api/v1/customers/{self.assigned.pk}/"
        self.assertEqual(client.patch(url, {"city": "X"}, format="json").status_code, 403)
        self.assertEqual(client.post("/api/v1/customers/", {"phone": "1"}).status_code, 403)

    def test_appointments_action_shows_only_own_bookings(self):
        rows = (
            self.as_user(self.provider)
            .get(f"/api/v1/customers/{self.treated.pk}/appointments/")
            .json()["results"]
        )
        self.assertEqual([r["id"] for r in rows], [str(self.booking.pk)])
        rows = (
            self.as_user(self.manager)
            .get(f"/api/v1/customers/{self.treated.pk}/appointments/")
            .json()["results"]
        )
        self.assertEqual(len(rows), 2)

    def test_own_notes(self):
        managers_internal = create_note(customer=self.assigned, author=self.manager, content="m")
        shared = create_note(
            customer=self.assigned, author=self.manager, content="s", visibility=VISIBLE
        )
        on_stranger = create_note(
            customer=self.stranger, author=self.manager, content="x", visibility=VISIBLE
        )
        client = self.as_user(self.provider)

        response = client.post(
            "/api/v1/customer-notes/",
            {
                "customer": str(self.assigned.pk),
                "content": "Tight shoulders",
                "visibility": "internal",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        own = response.data["id"]

        rows = client.get("/api/v1/customer-notes/").json()["results"]
        self.assertEqual({r["id"] for r in rows}, {own, str(shared.pk)})
        self.assertEqual(
            client.get(f"/api/v1/customer-notes/{managers_internal.pk}/").status_code, 404
        )
        self.assertEqual(client.get(f"/api/v1/customer-notes/{on_stranger.pk}/").status_code, 404)

        response = client.post(
            "/api/v1/customer-notes/",
            {"customer": str(self.stranger.pk), "content": "x", "visibility": "customer_visible"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        response = client.patch(
            f"/api/v1/customer-notes/{shared.pk}/", {"pinned": True}, format="json"
        )
        self.assertEqual(response.status_code, 403)  # not the author

        # The manager (customers.notes.private) sees the provider's internal note.
        rows = self.as_user(self.manager).get("/api/v1/customer-notes/").json()["results"]
        self.assertIn(own, {r["id"] for r in rows})


class MergeAndAnonymizeApiTests(Base):
    def setUp(self):
        super().setUp()
        self.receptionist = self.member(OrganizationRole.RECEPTIONIST)
        self.manager = self.member(OrganizationRole.MANAGER)
        self.target = self.customer(email="ada@example.test")
        self.duplicate = self.customer(email="ada.l@example.test")

    def test_receptionist_can_merge(self):
        f.BookingFactory(organization=self.organization, customer=self.duplicate)
        response = self.as_user(self.receptionist).post(
            f"/api/v1/customers/{self.target.pk}/merge/",
            {"duplicate": str(self.duplicate.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["stats"]["total_appointments"], 1)
        self.assertFalse(Customer.objects.filter(pk=self.duplicate.pk).exists())

    def test_merge_into_itself_is_400(self):
        response = self.as_user(self.receptionist).post(
            f"/api/v1/customers/{self.target.pk}/merge/",
            {"duplicate": str(self.target.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_erasing_needs_customers_erase(self):
        client = self.as_user(self.receptionist)
        url = f"/api/v1/customers/{self.target.pk}/"
        self.assertEqual(client.post(f"{url}anonymize/", {"confirm": True}).status_code, 403)
        self.assertEqual(client.delete(url).status_code, 403)
        self.assertTrue(Customer.objects.filter(pk=self.target.pk).exists())

    def test_anonymize_needs_explicit_confirmation(self):
        client = self.as_user(self.manager)
        url = f"/api/v1/customers/{self.target.pk}/anonymize/"
        self.assertEqual(client.post(url, {}, format="json").status_code, 400)
        self.assertEqual(client.post(url, {"confirm": False}, format="json").status_code, 400)
        response = client.post(url, {"confirm": True}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "anonymized")
        self.assertEqual(response.data["email"], "")
        self.assertTrue(AuditLog.objects.filter(action="customer.anonymized").exists())

    def test_manager_can_delete(self):
        response = self.as_user(self.manager).delete(f"/api/v1/customers/{self.duplicate.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(AuditLog.objects.filter(action="customer.deleted").exists())


class GlobalSearchTests(Base):
    def setUp(self):
        super().setUp()
        self.ada = self.customer(first_name="Ada", last_name="Morgan", phone="+14165550101")
        self.staff = f.StaffProfileFactory(
            organization=self.organization, user=f.UserFactory(first_name="Morgan", last_name="Lee")
        )
        self.service = f.ServiceFactory(organization=self.organization, name="Morgan massage")
        self.booking = f.BookingFactory(
            organization=self.organization,
            customer=self.ada,
            staff=self.staff,
            service=self.service,
            customer_name="Ada Morgan",
            reference="SCH-2099-424242",
        )

    def search(self, user, **params):
        return self.as_user(user).get("/api/v1/search/", params)

    def test_manager_sees_every_section(self):
        results = self.search(self.member(OrganizationRole.MANAGER), q="morgan").json()["results"]
        self.assertEqual([r["id"] for r in results["customers"]], [str(self.ada.pk)])
        self.assertEqual([r["id"] for r in results["appointments"]], [str(self.booking.pk)])
        self.assertEqual([r["id"] for r in results["staff"]], [str(self.staff.pk)])
        self.assertEqual([r["id"] for r in results["services"]], [str(self.service.pk)])

    def test_reference_and_phone(self):
        manager = self.member(OrganizationRole.MANAGER)
        results = self.search(manager, q="424242").json()["results"]
        self.assertEqual(results["appointments"][0]["reference"], "SCH-2099-424242")
        results = self.search(manager, q="(416) 555-0101").json()["results"]
        self.assertEqual(results["customers"][0]["id"], str(self.ada.pk))

    def test_sections_follow_capabilities(self):
        customer_member = self.member(OrganizationRole.CUSTOMER)
        results = self.search(customer_member, q="morgan").json()["results"]
        self.assertEqual(results["customers"], [])
        self.assertEqual(results["appointments"], [])  # not their booking
        self.assertNotIn("staff", results)
        self.assertNotIn("services", results)

        provider = self.member(OrganizationRole.STAFF)
        results = self.search(provider, q="morgan").json()["results"]
        self.assertEqual(results["customers"], [])  # not their customer
        self.assertEqual(results["appointments"], [])

    def test_limits_and_validation(self):
        manager = self.member(OrganizationRole.MANAGER)
        for _ in range(8):
            self.customer(last_name="Morgan")
        results = self.search(manager, q="morgan").json()["results"]
        self.assertEqual(len(results["customers"]), 5)
        results = self.search(manager, q="morgan", limit=100).json()["results"]
        self.assertEqual(len(results["customers"]), 9)
        self.assertEqual(self.search(manager, q="m").status_code, 400)
        self.assertEqual(self.search(manager, q="mo", limit="x").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/customers/search/", {"q": "a"}).status_code, 400)

    def test_non_members_are_refused(self):
        self.assertEqual(self.search(f.UserFactory(), q="morgan").status_code, 403)
