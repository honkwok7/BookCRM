"""M2.2: organization-defined customer tags."""

import importlib

from django.apps import apps
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from bookings.models import Customer
from core.exceptions import ConflictError, DomainError
from core.models import AuditLog
from crm.models import CustomerTag, Tag
from crm.selectors import list_customers
from crm.services import (
    add_customer_tag,
    anonymize_customer,
    create_tag,
    delete_tag,
    merge_customers,
    remove_customer_tag,
    set_customer_tags,
    update_tag,
)
from organizations.models import OrganizationRole
from tests import factories as f


class BackfillTests(TestCase):
    def test_json_tags_become_tags_per_organization(self):
        org_a, org_b = f.OrganizationFactory(), f.OrganizationFactory()
        ada = f.CustomerFactory(organization=org_a, tags=["VIP", "Prefers mornings"])
        bob = f.CustomerFactory(organization=org_a, tags=["vip", "", 42])
        nora = f.CustomerFactory(organization=org_b, tags=["VIP"])

        migration = importlib.import_module("crm.migrations.0002_backfill_tags_from_json")
        migration.backfill(apps, None)
        migration.backfill(apps, None)  # idempotent

        self.assertEqual(
            sorted(Tag.objects.filter(organization=org_a).values_list("slug", flat=True)),
            ["42", "prefers-mornings", "vip"],
        )
        vip_a = Tag.objects.get(organization=org_a, slug="vip")
        self.assertEqual(vip_a.name, "VIP")
        self.assertEqual(set(vip_a.customers.all()), {ada, bob})
        self.assertEqual(list(nora.tag_set.values_list("organization", flat=True)), [org_b.pk])
        self.assertEqual(CustomerTag.objects.count(), 5)


class TagServiceTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.customer = f.CustomerFactory(organization=self.organization)

    def test_create_derives_slug_and_refuses_duplicates(self):
        tag = create_tag(organization=self.organization, name=" New Patient ", color="#10b981")
        self.assertEqual((tag.name, tag.slug), ("New Patient", "new-patient"))
        with self.assertRaises(ConflictError):
            create_tag(organization=self.organization, name="new patient")
        create_tag(organization=f.OrganizationFactory(), name="New Patient")  # other tenant

    def test_invalid_names_and_colors(self):
        for kwargs in ({"name": "  "}, {"name": "!!!"}, {"name": "x" * 61}):
            with self.subTest(**kwargs), self.assertRaises(DomainError):
                create_tag(organization=self.organization, **kwargs)
        with self.assertRaises(DomainError):
            create_tag(organization=self.organization, name="VIP", color="red")

    def test_slug_is_unique_per_org_in_the_database(self):
        f.TagFactory(organization=self.organization, slug="vip")
        with self.assertRaises(IntegrityError), transaction.atomic():
            f.TagFactory(organization=self.organization, slug="vip")

    def test_rename_updates_slug_and_is_audited(self):
        tag = create_tag(organization=self.organization, name="VIP")
        create_tag(organization=self.organization, name="Gold")
        with self.assertRaises(ConflictError):
            update_tag(tag=tag, name="gold")
        update_tag(tag=tag, name="Platinum")
        self.assertEqual(tag.slug, "platinum")
        self.assertTrue(AuditLog.objects.filter(action="tag.updated").exists())

    def test_add_and_remove_are_idempotent_and_audited_with_ids_only(self):
        tag = create_tag(organization=self.organization, name="Diabetic")
        self.assertTrue(add_customer_tag(customer=self.customer, tag=tag))
        self.assertFalse(add_customer_tag(customer=self.customer, tag=tag))
        self.assertTrue(remove_customer_tag(customer=self.customer, tag=tag))
        self.assertFalse(remove_customer_tag(customer=self.customer, tag=tag))
        entries = AuditLog.objects.filter(action__startswith="customer.tag_")
        self.assertEqual(
            list(entries.order_by("created_at").values_list("action", flat=True)),
            ["customer.tag_added", "customer.tag_removed"],
        )
        self.assertNotIn("Diabetic", str(list(entries.values("metadata"))))

    def test_cross_tenant_tag_assignment_is_rejected(self):
        foreign = f.TagFactory()
        with self.assertRaises(DomainError) as raised:
            add_customer_tag(customer=self.customer, tag=foreign)
        self.assertEqual(raised.exception.code, "not_found")
        with self.assertRaises(DomainError):
            set_customer_tags(customer=self.customer, tags=[foreign])
        self.assertFalse(self.customer.tag_set.exists())

    def test_set_tags_adds_and_removes(self):
        vip, new, old = (f.TagFactory(organization=self.organization) for _ in range(3))
        add_customer_tag(customer=self.customer, tag=old)
        set_customer_tags(customer=self.customer, tags=[vip, new])
        self.assertEqual(set(self.customer.tag_set.all()), {vip, new})

    def test_delete_tag_untags_everyone(self):
        tag = f.CustomerTagFactory(organization=self.organization, customer=self.customer).tag
        delete_tag(tag=tag)
        self.assertFalse(self.customer.tag_set.exists())
        entry = AuditLog.objects.get(action="tag.deleted")
        self.assertEqual(entry.metadata["customers"], 1)

    def test_merge_combines_tags_and_anonymize_removes_them(self):
        vip, member = (f.TagFactory(organization=self.organization) for _ in range(2))
        duplicate = f.CustomerFactory(organization=self.organization)
        add_customer_tag(customer=self.customer, tag=vip)
        add_customer_tag(customer=duplicate, tag=vip)
        add_customer_tag(customer=duplicate, tag=member)
        merged = merge_customers(target=self.customer, duplicate=duplicate)
        self.assertEqual(set(merged.tag_set.all()), {vip, member})

        anonymize_customer(customer=merged)
        self.assertFalse(merged.tag_set.exists())
        with self.assertRaises(ConflictError):
            add_customer_tag(customer=merged, tag=vip)

    def test_filter_customers_by_tag(self):
        tag = f.TagFactory(organization=self.organization)
        add_customer_tag(customer=self.customer, tag=tag)
        f.CustomerFactory(organization=self.organization)
        self.assertEqual(list(list_customers(self.organization, tag=tag)), [self.customer])


class TagApiTests(TestCase):
    def setUp(self):
        self.organization = f.OrganizationFactory()
        self.client = APIClient()
        self.receptionist = f.MembershipFactory(
            organization=self.organization, role=OrganizationRole.RECEPTIONIST
        ).user
        self.client.force_authenticate(self.receptionist)

    def test_tag_crud(self):
        response = self.client.post("/api/v1/tags/", {"name": "VIP", "color": "#f59e0b"})
        self.assertEqual(response.status_code, 201, response.data)
        tag_id = response.data["id"]
        self.assertEqual(response.data["slug"], "vip")
        self.assertEqual(self.client.post("/api/v1/tags/", {"name": "vip"}).status_code, 409)

        response = self.client.patch(f"/api/v1/tags/{tag_id}/", {"name": "Gold"}, format="json")
        self.assertEqual(response.data["slug"], "gold")
        self.assertEqual(self.client.delete(f"/api/v1/tags/{tag_id}/").status_code, 204)
        self.assertEqual(
            list(AuditLog.objects.order_by("created_at").values_list("action", flat=True)),
            ["tag.created", "tag.updated", "tag.deleted"],
        )

    def test_customer_count_ignores_anonymized_customers(self):
        tag = f.TagFactory(organization=self.organization)
        f.CustomerTagFactory(organization=self.organization, tag=tag)
        f.CustomerTagFactory(
            organization=self.organization,
            tag=tag,
            customer__status=Customer.Status.ANONYMIZED,
        )
        body = self.client.get(f"/api/v1/tags/{tag.pk}/").json()
        self.assertEqual(body["customer_count"], 1)

    def test_staff_can_read_but_not_manage_tags(self):
        staff = f.MembershipFactory(organization=self.organization, role=OrganizationRole.STAFF)
        self.client.force_authenticate(staff.user)
        self.assertEqual(self.client.post("/api/v1/tags/", {"name": "VIP"}).status_code, 403)

    def test_tag_ids_set_the_customer_tags_and_tags_are_returned(self):
        vip, new = (f.TagFactory(organization=self.organization) for _ in range(2))
        response = self.client.post(
            "/api/v1/customers/",
            {"first_name": "Ada", "phone": "+15550100", "tag_ids": [str(vip.pk), str(new.pk)]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual({t["name"] for t in response.data["tags"]}, {vip.name, new.name})

        customer_id = response.data["id"]
        response = self.client.patch(
            f"/api/v1/customers/{customer_id}/", {"tag_ids": [str(vip.pk)]}, format="json"
        )
        self.assertEqual([t["id"] for t in response.data["tags"]], [str(vip.pk)])
        response = self.client.patch(
            f"/api/v1/customers/{customer_id}/", {"tag_ids": []}, format="json"
        )
        self.assertEqual(response.data["tags"], [])

    def test_filter_customers_by_tag(self):
        tag = f.TagFactory(organization=self.organization)
        tagged = f.CustomerTagFactory(organization=self.organization, tag=tag).customer
        f.CustomerFactory(organization=self.organization)
        rows = self.client.get("/api/v1/customers/", {"tag": str(tag.pk)}).json()["results"]
        self.assertEqual([row["id"] for row in rows], [str(tagged.pk)])
