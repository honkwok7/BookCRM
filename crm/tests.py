"""M2.1: CRM customer model, service and selectors."""

import importlib
from datetime import timedelta

from django.apps import apps
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking, BookingActivityLog, BookingStatusHistory, Customer
from bookings.services import create_booking, reschedule_booking
from core.exceptions import ConflictError, DomainError
from core.models import AuditLog
from crm.selectors import customer_stats, list_customers
from crm.services import (
    anonymize_customer,
    create_customer,
    find_or_create_customer,
    merge_customers,
    update_customer,
)
from notifications.models import NotificationLog
from organizations.models import OrganizationRole
from tests import factories as f


class NameTests(TestCase):
    def test_split_rule(self):
        self.assertEqual(Customer.split_name("Mary Ann Smith"), ("Mary Ann", "Smith"))
        self.assertEqual(Customer.split_name("Cher"), ("Cher", ""))
        self.assertEqual(Customer.split_name("  "), ("", ""))

    def test_backfill_migration_splits_existing_names(self):
        customer = f.CustomerFactory()
        Customer.objects.filter(pk=customer.pk).update(
            name="Mary Ann Smith", first_name="", last_name=""
        )
        migration = importlib.import_module("bookings.migrations.0003_customer_split_names")
        migration.split_names(apps, None)
        customer.refresh_from_db()
        self.assertEqual((customer.first_name, customer.last_name), ("Mary Ann", "Smith"))
        self.assertEqual(customer.name, "Mary Ann Smith")

    def test_name_stays_in_sync(self):
        customer = f.CustomerFactory(first_name="Ada", last_name="Lovelace")
        self.assertEqual(customer.name, "Ada Lovelace")
        customer.last_name = "Byron"
        customer.save(update_fields=["last_name"])
        customer.refresh_from_db()
        self.assertEqual(customer.name, "Ada Byron")

    def test_display_name_prefers_preferred_name(self):
        customer = f.CustomerFactory(first_name="Robert", last_name="Ng", preferred_name="Bob")
        self.assertEqual(customer.display_name, "Bob")


class ConstraintTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()

    def test_email_or_phone_required_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            f.CustomerFactory(organization=self.organization, email="", phone="")

    def test_phone_only_customer_allowed(self):
        f.CustomerFactory(organization=self.organization, email="", phone="+15550100")

    def test_anonymized_customer_needs_no_contact(self):
        f.CustomerFactory(
            organization=self.organization, email="", phone="", status=Customer.Status.ANONYMIZED
        )

    def test_email_unique_per_org_case_insensitive(self):
        f.CustomerFactory(organization=self.organization, email="Sam@Example.test")
        with self.assertRaises(IntegrityError), transaction.atomic():
            f.CustomerFactory(organization=self.organization, email="sam@example.test")

    def test_blank_emails_do_not_collide(self):
        f.CustomerFactory(organization=self.organization, email="", phone="+15550101")
        f.CustomerFactory(organization=self.organization, email="", phone="+15550102")

    def test_same_email_in_two_organizations(self):
        f.CustomerFactory(organization=self.organization, email="sam@example.test")
        f.CustomerFactory(email="sam@example.test")


class ServiceTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.actor = f.MembershipFactory(organization=self.organization).user

    def create(self, **fields):
        fields = {"first_name": "Ada", "last_name": "Lovelace", **fields}
        return create_customer(organization=self.organization, actor=self.actor, **fields)

    def test_create_requires_contact_and_name(self):
        with self.assertRaises(DomainError) as raised:
            self.create()
        self.assertEqual(raised.exception.code, "contact_required")
        with self.assertRaises(DomainError) as raised:
            self.create(first_name="", last_name="", phone="+15550100")
        self.assertEqual(raised.exception.code, "name_required")

    def test_create_refuses_duplicate_email(self):
        self.create(email="ada@example.test")
        with self.assertRaises(ConflictError) as raised:
            self.create(email="ADA@example.test")
        self.assertEqual(raised.exception.code, "duplicate_email")

    def test_create_stamps_consent_and_audits(self):
        customer = self.create(phone="+15550100", sms_consent=True)
        self.assertIsNotNone(customer.consent_updated_at)
        self.assertEqual(customer.created_by, self.actor)
        self.assertTrue(
            AuditLog.objects.filter(action="customer.created", object_identifier=customer.pk)
        )

    def test_update_consent_is_stamped_and_audited_separately(self):
        customer = self.create(email="ada@example.test")
        update_customer(customer=customer, actor=self.actor, marketing_consent=True)
        customer.refresh_from_db()
        self.assertIsNotNone(customer.consent_updated_at)
        entry = AuditLog.objects.get(action="customer.consent_changed")
        self.assertEqual(entry.metadata, {"marketing_consent": True})
        self.assertEqual(entry.user, self.actor)

    def test_update_audit_redacts_personal_data(self):
        customer = self.create(email="ada@example.test")
        update_customer(customer=customer, phone="+15559999", city="Toronto", status="inactive")
        changes = AuditLog.objects.get(action="customer.updated").metadata["changes"]
        self.assertNotIn("+15559999", str(changes))
        self.assertNotIn("Toronto", str(changes))
        self.assertIn("phone", changes)
        self.assertEqual(changes["status"], ["active", "inactive"])

    def test_update_rules(self):
        customer = self.create(email="ada@example.test")
        with self.assertRaises(DomainError):
            update_customer(customer=customer, email="")  # would leave no contact
        with self.assertRaises(DomainError):
            update_customer(customer=customer, status=Customer.Status.ANONYMIZED)
        with self.assertRaises(DomainError):
            update_customer(customer=customer, organization=f.OrganizationFactory())
        with self.assertRaises(DomainError):
            update_customer(customer=customer, assigned_staff=f.StaffProfileFactory())

    def test_find_or_create_matches_email_then_phone(self):
        by_email = self.create(email="ada@example.test", phone="+15550100")
        by_phone = self.create(first_name="Leo", phone="+15550200")
        found = find_or_create_customer(
            organization=self.organization, name="X", email="ADA@example.test"
        )
        self.assertEqual(found, by_email)
        found = find_or_create_customer(organization=self.organization, name="X", phone="+15550200")
        self.assertEqual(found, by_phone)

    def test_find_or_create_never_links_a_user_to_an_existing_record(self):
        existing = self.create(email="ada@example.test")
        stranger = f.UserFactory()
        found = find_or_create_customer(
            organization=self.organization, name="Ada", email="ada@example.test", user=stranger
        )
        self.assertEqual(found, existing)
        found.refresh_from_db()
        self.assertIsNone(found.user)

    def test_find_or_create_links_the_user_on_a_new_record(self):
        user = f.UserFactory()
        customer = find_or_create_customer(
            organization=self.organization, name="New Person", email="new@example.test", user=user
        )
        self.assertEqual(customer.user, user)
        self.assertEqual((customer.first_name, customer.last_name), ("New", "Person"))


class MergeTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.target = f.CustomerFactory(organization=self.organization, city="")
        self.duplicate = f.CustomerFactory(
            organization=self.organization, city="Toronto", marketing_consent=True
        )
        self.booking = f.BookingFactory(organization=self.organization, customer=self.duplicate)

    def test_merge_moves_appointments_and_fills_blanks(self):
        merged = merge_customers(target=self.target, duplicate=self.duplicate)
        self.assertFalse(Customer.objects.filter(pk=self.duplicate.pk).exists())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.customer, merged)
        self.assertEqual(merged.city, "Toronto")
        # Notes are CustomerNote rows since review fix F1; merging moves them
        # (crm/test_notes_activity.py).
        self.assertFalse(merged.marketing_consent)  # consent is never copied
        entry = AuditLog.objects.get(action="customer.merged")
        self.assertEqual(entry.metadata["appointments_moved"], 1)

    def test_merge_refuses_cross_org_self_and_conflicting_accounts(self):
        with self.assertRaises(DomainError):
            merge_customers(target=self.target, duplicate=f.CustomerFactory())
        with self.assertRaises(DomainError):
            merge_customers(target=self.target, duplicate=self.target)
        self.target.user = f.UserFactory()
        self.target.save()
        self.duplicate.user = f.UserFactory()
        self.duplicate.save()
        with self.assertRaises(ConflictError):
            merge_customers(target=self.target, duplicate=self.duplicate)


class AnonymizationTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.customer = f.CustomerFactory(
            organization=self.organization,
            first_name="Grace",
            last_name="Hopper",
            email="grace@example.test",
            phone="+15550100",
            city="Arlington",
            notes="Allergic to latex",
            user=f.UserFactory(),
            sms_consent=True,
        )
        self.booking = f.BookingFactory(
            organization=self.organization,
            customer=self.customer,
            customer_notes="Grace's note",
            status=Booking.Status.COMPLETED,
            price_snapshot=120,
        )
        BookingStatusHistory.objects.create(booking=self.booking, new_status="done", note="Grace")
        BookingActivityLog.objects.create(
            booking=self.booking,
            organization=self.organization,
            action="booking.cancelled",
            metadata={"reason": "Grace is sick", "service": "x"},
        )
        f.NotificationLogFactory(
            organization=self.organization,
            related_booking=self.booking,
            recipient_email="grace@example.test",
        )
        f.WaitlistEntryFactory(organization=self.organization, customer_email="GRACE@example.test")

    def test_anonymization_removes_personal_data_everywhere(self):
        anonymize_customer(customer=self.customer)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.status, Customer.Status.ANONYMIZED)
        self.assertEqual(self.customer.name, "Anonymized")
        for field in ("email", "phone", "city", "notes", "last_name"):
            self.assertEqual(getattr(self.customer, field), "", field)
        self.assertIsNone(self.customer.user)
        self.assertFalse(self.customer.sms_consent)
        self.assertIsNotNone(self.customer.anonymized_at)

        dump = str(
            [
                list(Booking.objects.values()),
                list(BookingStatusHistory.objects.values()),
                list(BookingActivityLog.objects.values()),
                list(NotificationLog.objects.values()),
                list(Customer.objects.values()),
                list(self.organization.waitlist_entries.values()),
                list(AuditLog.objects.values()),
            ]
        )
        for secret in ("Grace", "grace@", "GRACE@", "Hopper", "+15550100", "Arlington", "latex"):
            self.assertNotIn(secret, dump)

    def test_anonymization_keeps_aggregates(self):
        before = customer_stats(self.customer)
        anonymize_customer(customer=self.customer)
        self.assertEqual(customer_stats(self.customer), before)
        self.assertEqual(before["lifetime_value"], 120)

    def test_anonymized_customer_is_frozen_and_hidden_by_default(self):
        anonymize_customer(customer=self.customer)
        with self.assertRaises(ConflictError):
            update_customer(customer=self.customer, first_name="Back")
        self.assertNotIn(self.customer, list_customers(self.organization))
        self.assertIn(self.customer, list_customers(self.organization, include_anonymized=True))
        anonymize_customer(customer=self.customer)  # idempotent
        self.assertEqual(AuditLog.objects.filter(action="customer.anonymized").count(), 1)


class StatsAndSelectorTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.staff = f.StaffProfileFactory(organization=self.organization)
        self.service = f.ServiceFactory(organization=self.organization, price=100)
        f.make_bookable(self.staff, self.service)

    def test_stats_count_reschedules_once(self):
        start = f.future(3)
        booking = create_booking(
            organization=self.organization,
            service=self.service,
            staff_profile=self.staff,
            customer_name="Ada Lovelace",
            customer_email="ada@example.test",
            customer_phone="",
            start_datetime=start,
            notify=False,
        )
        reschedule_booking(booking=booking, new_start=start + timedelta(hours=3))
        f.BookingFactory(
            organization=self.organization,
            customer=booking.customer,
            status=Booking.Status.COMPLETED,
            price_snapshot=100,
            start_datetime=timezone.now() - timedelta(days=10),
        )
        stats = customer_stats(booking.customer)
        self.assertEqual(stats["total_appointments"], 2)
        self.assertEqual(stats["cancelled"], 0)
        self.assertEqual(stats["upcoming"], 1)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["lifetime_value"], 100)
        self.assertEqual(stats["next_appointment"], start + timedelta(hours=3))

    def test_booking_engine_creates_crm_customer_with_source(self):
        booking = create_booking(
            organization=self.organization,
            service=self.service,
            staff_profile=self.staff,
            customer_name="Mary Ann Smith",
            customer_email="mary@example.test",
            customer_phone="",
            start_datetime=f.future(2),
            source=Booking.Source.RECEPTION,
            notify=False,
        )
        customer = booking.customer
        self.assertEqual((customer.first_name, customer.last_name), ("Mary Ann", "Smith"))
        self.assertEqual(customer.source, Customer.Source.RECEPTION)

    def test_search_matches_every_term(self):
        f.CustomerFactory(organization=self.organization, first_name="Ada", last_name="Lovelace")
        f.CustomerFactory(organization=self.organization, first_name="Ada", last_name="Byron")
        rows = list_customers(self.organization, search="ada love")
        self.assertEqual([c.last_name for c in rows], ["Lovelace"])


class CustomerApiTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.user = f.MembershipFactory(
            organization=self.organization, role=OrganizationRole.RECEPTIONIST
        ).user
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_create_phone_only_customer(self):
        response = self.client.post(
            "/api/v1/customers/",
            {"first_name": "Leo", "last_name": "Martin", "phone": "+15550107", "sms_consent": True},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["name"], "Leo Martin")
        self.assertEqual(response.data["created_by"], self.user.pk)
        self.assertIsNotNone(response.data["consent_updated_at"])
        self.assertEqual(AuditLog.objects.filter(action="customer.created").count(), 1)

    def test_create_without_contact_is_400(self):
        response = self.client.post("/api/v1/customers/", {"first_name": "Leo"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "contact_required")

    def test_status_cannot_be_set_to_anonymized(self):
        customer = f.CustomerFactory(organization=self.organization)
        response = self.client.patch(
            f"/api/v1/customers/{customer.pk}/", {"status": "anonymized"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_update_is_audited_once(self):
        customer = f.CustomerFactory(organization=self.organization)
        response = self.client.patch(
            f"/api/v1/customers/{customer.pk}/", {"alerts": "Uses a wheelchair"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(AuditLog.objects.filter(action="customer.updated").count(), 1)

    def test_retrieve_includes_stats(self):
        customer = f.CustomerFactory(organization=self.organization)
        body = self.client.get(f"/api/v1/customers/{customer.pk}/").json()
        self.assertEqual(body["stats"]["total_appointments"], 0)

    def test_assigned_staff_must_belong_to_the_organization(self):
        customer = f.CustomerFactory(organization=self.organization)
        foreign_staff = f.StaffProfileFactory()
        response = self.client.patch(
            f"/api/v1/customers/{customer.pk}/",
            {"assigned_staff": str(foreign_staff.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        own_staff = f.StaffProfileFactory(organization=self.organization)
        response = self.client.patch(
            f"/api/v1/customers/{customer.pk}/",
            {"assigned_staff": str(own_staff.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_filter_by_status(self):
        f.CustomerFactory(organization=self.organization, status=Customer.Status.INACTIVE)
        f.CustomerFactory(organization=self.organization)
        rows = self.client.get("/api/v1/customers/", {"status": "inactive"}).json()["results"]
        self.assertEqual([row["status"] for row in rows], ["inactive"])

    def test_full_name_is_accepted_and_split(self):
        response = self.client.post(
            "/api/v1/customers/", {"name": "Mary Ann Smith", "phone": "+15550300"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            (response.data["first_name"], response.data["last_name"]), ("Mary Ann", "Smith")
        )
        customer_id = response.data["id"]
        response = self.client.patch(
            f"/api/v1/customers/{customer_id}/", {"name": "Mary Jones"}, format="json"
        )
        self.assertEqual(response.data["name"], "Mary Jones")
