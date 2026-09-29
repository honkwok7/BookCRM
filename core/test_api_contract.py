"""M1.3 API contract and tenant-safety tests."""

import importlib
import inspect
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework import serializers
from rest_framework.test import APITestCase

from bookings.models import Booking, BookingStatusHistory, Customer
from bookings.services import create_booking
from core.api import TenantMemberUserField, TenantPrimaryKeyRelatedField
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from scheduling.models import WeeklyAvailability
from services.models import Service, ServiceCategory
from staff.models import StaffProfile

User = get_user_model()

TENANT_SERIALIZER_MODULES = (
    "bookings.serializers",
    "crm.serializers",
    "locations.serializers",
    "scheduling.serializers",
    "services.serializers",
    "staff.serializers",
)
SAFE_RELATION_FIELDS = (TenantPrimaryKeyRelatedField, TenantMemberUserField)


def tenant_model_serializers():
    for module_name in TENANT_SERIALIZER_MODULES:
        module = importlib.import_module(module_name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, serializers.ModelSerializer) and obj.__module__ == module_name:
                yield obj


class SerializerContractTests(SimpleTestCase):
    def test_serializers_are_discovered(self):
        self.assertGreaterEqual(len(list(tenant_model_serializers())), 10)

    def test_no_serializer_uses_all_fields(self):
        for serializer_class in tenant_model_serializers():
            with self.subTest(serializer=serializer_class.__name__):
                self.assertNotEqual(serializer_class.Meta.fields, "__all__")

    def test_every_writable_relation_is_tenant_limited(self):
        for serializer_class in tenant_model_serializers():
            for name, field in serializer_class().fields.items():
                if field.read_only:
                    continue
                relation = (
                    field.child_relation
                    if isinstance(field, serializers.ManyRelatedField)
                    else field
                )
                if isinstance(relation, serializers.RelatedField):
                    with self.subTest(serializer=serializer_class.__name__, field=name):
                        self.assertIsInstance(relation, SAFE_RELATION_FIELDS)

    def test_organization_is_never_writable(self):
        for serializer_class in tenant_model_serializers():
            field = serializer_class().fields.get("organization")
            if field is not None:
                with self.subTest(serializer=serializer_class.__name__):
                    self.assertTrue(field.read_only)


class TwoTenants(APITestCase):
    def setUp(self):
        self.org_a = Organization.objects.create(name="A", slug="org-a")
        self.org_b = Organization.objects.create(name="B", slug="org-b")
        self.manager_a = self.member(self.org_a, "manager@a.test", OrganizationRole.MANAGER)
        self.receptionist_a = self.member(
            self.org_a, "reception@a.test", OrganizationRole.RECEPTIONIST
        )
        self.provider_user = self.member(self.org_a, "provider@a.test", OrganizationRole.STAFF)
        self.staff_a = StaffProfile.objects.create(user=self.provider_user, organization=self.org_a)
        self.staff_b = StaffProfile.objects.create(
            user=User.objects.create_user(email="provider@b.test", password="Password12345!"),
            organization=self.org_b,
        )
        self.service_a = Service.objects.create(
            organization=self.org_a, name="A", slug="a", price=10, duration_minutes=30
        )
        self.service_b = Service.objects.create(
            organization=self.org_b, name="B", slug="b", price=10, duration_minutes=30
        )
        self.category_b = ServiceCategory.objects.create(
            organization=self.org_b, name="B", slug="b"
        )

    @staticmethod
    def member(organization, email, role):
        user = User.objects.create_user(email=email, password="Password12345!")
        OrganizationMembership.objects.create(organization=organization, user=user, role=role)
        return user

    def book(self, start=None, email="customer@example.test"):
        return create_booking(
            organization=self.org_a,
            service=self.service_a,
            staff_profile=self.staff_a,
            customer_name="Customer",
            customer_email=email,
            customer_phone="",
            start_datetime=start or timezone.now() + timedelta(days=2),
        )


class CrossTenantReferenceTests(TwoTenants):
    """Regression tests for probes P4 and P5 in docs/CURRENT_STATE_ANALYSIS.md."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager_a)

    def test_availability_for_other_tenant_staff_rejected(self):
        response = self.client.post(
            "/api/v1/availability/weekly/",
            {
                "staff": str(self.staff_b.id),
                "day_of_week": 1,
                "start_time": "09:00",
                "end_time": "10:00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("staff", response.json())
        self.assertFalse(WeeklyAvailability.objects.filter(staff=self.staff_b).exists())

    def test_assigning_other_tenant_staff_to_service_rejected(self):
        response = self.client.patch(
            f"/api/v1/services/{self.service_a.id}/",
            {"assigned_staff_members": [str(self.staff_b.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.service_a.assigned_staff_members.exists())

    def test_other_tenant_category_rejected(self):
        response = self.client.patch(
            f"/api/v1/services/{self.service_a.id}/",
            {"category": str(self.category_b.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_own_tenant_references_accepted(self):
        response = self.client.patch(
            f"/api/v1/services/{self.service_a.id}/",
            {"assigned_staff_members": [str(self.staff_a.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_waitlist_entry_for_other_tenant_service_rejected(self):
        response = self.client.post(
            "/api/v1/waitlist/",
            {
                "service": str(self.service_b.id),
                "customer_name": "X",
                "customer_email": "x@example.test",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_staff_profile_requires_org_member(self):
        outsider = User.objects.create_user(
            email="outsider@example.test", password="Password12345!"
        )
        response = self.client.post("/api/v1/staff/", {"user": outsider.pk}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("user", response.json())

    def test_staff_profile_for_member_accepted(self):
        response = self.client.post(
            "/api/v1/staff/", {"user": self.receptionist_a.pk}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["organization"], str(self.org_a.id))

    def test_customer_create_sets_org_and_rejects_duplicate_email(self):
        first = self.client.post(
            "/api/v1/customers/", {"name": "Jo", "email": "jo@example.test"}, format="json"
        )
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(first.json()["organization"], str(self.org_a.id))
        duplicate = self.client.post(
            "/api/v1/customers/", {"name": "Jo", "email": "JO@example.test"}, format="json"
        )
        self.assertEqual(duplicate.status_code, 400)

    def test_customer_user_link_is_read_only(self):
        response = self.client.post(
            "/api/v1/customers/",
            {"name": "Jo", "email": "jo@example.test", "user": self.manager_a.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(Customer.objects.get(email="jo@example.test").user)


class BookingApiTests(TwoTenants):
    """Regression for probe P6 plus the booking lifecycle through the service layer."""

    def setUp(self):
        super().setUp()
        self.booking = self.book()

    def test_generic_update_and_delete_are_gone(self):
        self.client.force_authenticate(self.manager_a)
        url = f"/api/v1/bookings/{self.booking.id}/"
        self.assertEqual(
            self.client.patch(url, {"status": "completed"}, format="json").status_code, 405
        )
        self.assertEqual(self.client.put(url, {}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)

    def test_customer_cannot_force_status(self):
        customer = User.objects.create_user(
            email="customer@example.test", password="Password12345!", email_verified=True
        )
        self.client.force_authenticate(customer)
        response = self.client.post(
            f"/api/v1/bookings/{self.booking.id}/update_status/",
            {"status": "completed"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_customer_does_not_see_internal_notes(self):
        Booking.objects.filter(pk=self.booking.pk).update(internal_notes="Difficult client")
        customer = User.objects.create_user(
            email="customer@example.test", password="Password12345!", email_verified=True
        )
        self.client.force_authenticate(customer)
        body = self.client.get(f"/api/v1/bookings/{self.booking.id}/").json()
        self.assertNotIn("internal_notes", body)
        self.client.force_authenticate(self.receptionist_a)
        body = self.client.get(f"/api/v1/bookings/{self.booking.id}/").json()
        self.assertEqual(body["internal_notes"], "Difficult client")

    def test_valid_transition_records_history(self):
        self.client.force_authenticate(self.receptionist_a)
        url = f"/api/v1/bookings/{self.booking.id}/update_status/"
        response = self.client.post(url, {"status": "checked_in"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        history = BookingStatusHistory.objects.filter(booking=self.booking).order_by("created_at")
        self.assertEqual(history.last().new_status, "checked_in")
        self.assertEqual(history.last().changed_by, self.receptionist_a)

    def test_illegal_transition_is_409(self):
        self.client.force_authenticate(self.receptionist_a)
        url = f"/api/v1/bookings/{self.booking.id}/update_status/"
        self.client.post(url, {"status": "no_show"}, format="json")
        response = self.client.post(url, {"status": "confirmed"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "invalid_transition")

    def test_unknown_status_is_400(self):
        self.client.force_authenticate(self.receptionist_a)
        response = self.client.post(
            f"/api/v1/bookings/{self.booking.id}/update_status/",
            {"status": "teleported"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_completed_booking_cannot_be_cancelled(self):
        self.client.force_authenticate(self.receptionist_a)
        base = f"/api/v1/bookings/{self.booking.id}/"
        for step in ("checked_in", "completed"):
            self.client.post(base + "update_status/", {"status": step}, format="json")
        response = self.client.post(base + "cancel/", {"reason": "oops"}, format="json")
        self.assertEqual(response.status_code, 409)

    def test_booking_conflict_is_409(self):
        self.client.force_authenticate(self.receptionist_a)
        response = self.client.post(
            "/api/v1/bookings/",
            {
                "service": str(self.service_a.id),
                "staff": str(self.staff_a.id),
                "start_datetime": self.booking.start_datetime.isoformat(),
                "customer_name": "Late",
                "customer_email": "late@example.test",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "slot_unavailable")

    def test_reschedule_moves_booking_and_links_it(self):
        self.client.force_authenticate(self.receptionist_a)
        new_start = self.booking.start_datetime + timedelta(hours=3)
        response = self.client.post(
            f"/api/v1/bookings/{self.booking.id}/reschedule/",
            {"start_datetime": new_start.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        new = Booking.objects.get(id=response.json()["id"])
        self.assertEqual(new.rescheduled_from_id, self.booking.id)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)

    def test_failed_reschedule_leaves_original_untouched(self):
        other = self.book(
            start=self.booking.start_datetime + timedelta(hours=5), email="o@example.test"
        )
        self.client.force_authenticate(self.receptionist_a)
        response = self.client.post(
            f"/api/v1/bookings/{self.booking.id}/reschedule/",
            {"start_datetime": other.start_datetime.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(Booking.objects.filter(rescheduled_from=self.booking).count(), 0)

    def test_reschedule_to_overlapping_own_slot_is_allowed(self):
        self.client.force_authenticate(self.receptionist_a)
        response = self.client.post(
            f"/api/v1/bookings/{self.booking.id}/reschedule/",
            {"start_datetime": (self.booking.start_datetime + timedelta(minutes=15)).isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_reschedule_keeps_customer_account_link(self):
        customer_user = User.objects.create_user(
            email="linked@example.test", password="Password12345!"
        )
        linked = create_booking(
            organization=self.org_a,
            service=self.service_a,
            staff_profile=self.staff_a,
            customer_name="Linked",
            customer_email="linked@example.test",
            customer_phone="",
            start_datetime=timezone.now() + timedelta(days=4),
            customer_user=customer_user,
        )
        self.client.force_authenticate(self.receptionist_a)
        response = self.client.post(
            f"/api/v1/bookings/{linked.id}/reschedule/",
            {"start_datetime": (linked.start_datetime + timedelta(hours=2)).isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        new = Booking.objects.get(id=response.json()["id"])
        self.assertEqual(new.customer.user, customer_user)
