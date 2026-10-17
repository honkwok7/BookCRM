"""Providers a customer asked not to be booked with (crm.services.set_avoided_providers)."""

import json
from datetime import UTC, datetime, time

from django.test import TestCase

from bookings.models import Booking
from bookings.services import create_booking, reschedule_booking
from core.audit import AuditAction
from core.exceptions import ConflictError, DomainError
from core.models import AuditLog
from crm.services import anonymize_customer, merge_customers, set_avoided_providers
from tests import factories as f
from tests.test_appointment_screens import HTMX, Fixtures

URL = "/app/appointments/new/"


class AvoidServiceTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()

    def book(self, staff, **kwargs):
        return create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=staff,
            customer=self.customer,
            customer_name=self.customer.name,
            customer_email=self.customer.email,
            start_datetime=datetime.combine(self.day, time(10), tzinfo=UTC),
            notify=False,
            **kwargs,
        )

    def test_set_is_audited_and_refuses_other_organizations(self):
        set_avoided_providers(
            customer=self.customer, staff=[self.maya], reason="Asked", actor=self.receptionist
        )
        self.assertEqual(list(self.customer.avoided_staff.all()), [self.maya])
        entry = AuditLog.objects.filter(action=AuditAction.CUSTOMER_UPDATED).latest("created_at")
        self.assertEqual(entry.metadata["changes"]["avoided_staff"], [[], [str(self.maya.pk)]])
        self.assertNotIn("Asked", json.dumps(entry.metadata))  # the reason stays off the log
        with self.assertRaises(DomainError):
            set_avoided_providers(customer=self.customer, staff=[f.StaffProfileFactory()])

    def test_booking_an_avoided_provider_is_refused(self):
        set_avoided_providers(customer=self.customer, staff=[self.maya])
        with self.assertRaises(ConflictError) as caught:
            self.book(self.maya)
        self.assertEqual(caught.exception.code, "provider_avoided")
        self.assertIn("isn't available for this customer", caught.exception.message)
        self.assertEqual(self.book(self.sam).staff, self.sam)

    def test_online_booking_gets_a_neutral_message(self):
        set_avoided_providers(customer=self.customer, staff=[self.maya])
        with self.assertRaises(ConflictError) as caught:
            self.book(self.maya, public=True, source=Booking.Source.PUBLIC_BOOKING)
        self.assertEqual(
            caught.exception.message,
            "This provider isn't available for you. Please choose another provider.",
        )

    def test_existing_appointments_can_still_be_rescheduled(self):
        booking = self.book(self.maya)
        set_avoided_providers(customer=self.customer, staff=[self.maya])
        moved = reschedule_booking(
            booking=booking, new_start=datetime.combine(self.day, time(13), tzinfo=UTC)
        )
        self.assertEqual(moved.staff, self.maya)

    def test_merge_keeps_and_anonymize_clears(self):
        duplicate = f.CustomerFactory(organization=self.org, email="ada2@x.test")
        set_avoided_providers(customer=duplicate, staff=[self.sam], reason="Prefers Maya")
        merge_customers(target=self.customer, duplicate=duplicate)
        self.customer.refresh_from_db()
        self.assertEqual(list(self.customer.avoided_staff.all()), [self.sam])
        self.assertEqual(self.customer.avoid_reason, "Prefers Maya")
        anonymize_customer(customer=self.customer)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.avoided_staff.exists())
        self.assertEqual(self.customer.avoid_reason, "")


class AvoidScreenTests(Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        set_avoided_providers(customer=self.customer, staff=[self.maya], reason="Asked at desk")
        self.client.force_login(self.receptionist)

    def test_the_new_appointment_form_leaves_them_out(self):
        data = {**self.payload(staff=""), "start": "", "refresh": "1"}
        response = self.client.post(URL, data, **HTMX)
        self.assertNotContains(response, f'<option value="{self.maya.pk}"')
        self.assertContains(response, f'<option value="{self.sam.pk}" selected>Sam</option>')
        self.assertContains(response, "asked not to be booked with Maya")

    def test_first_available_books_someone_else(self):
        lee = f.StaffProfileFactory(organization=self.org, display_name="Lee")
        f.make_bookable(lee, self.service, start=time(9), end=time(17))
        start = self.slot(self.sam)  # Maya, Sam and Lee are all free then
        data = self.payload(staff="any", start=start.isoformat())
        for _ in range(2):
            response = self.client.post(URL, data, **HTMX)
            self.assertEqual(response.status_code, 204, response.content)
        staff = set(Booking.objects.filter(customer=self.customer).values_list("staff", flat=True))
        self.assertEqual(staff, {self.sam.pk, lee.pk})
        # Only Maya is still free then: the time isn't offered to this customer any more.
        self.assertEqual(self.client.post(URL, data, **HTMX).status_code, 422)
        self.assertFalse(Booking.objects.filter(customer=self.customer, staff=self.maya).exists())

    def test_front_desk_sees_and_edits_the_list(self):
        page = self.client.get(f"/app/customers/{self.customer.pk}/")
        self.assertContains(page, "Providers to avoid")
        self.assertContains(page, "Asked at desk")
        url = f"/app/customers/{self.customer.pk}/avoid/"
        response = self.client.post(url, {"staff": [str(self.sam.pk)], "reason": ""}, **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(list(self.customer.avoided_staff.all()), [self.sam])

    def test_providers_never_see_it(self):
        create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.sam,
            customer=self.customer,
            customer_name=self.customer.name,
            customer_email=self.customer.email,
            start_datetime=self.slot(self.sam),
            notify=False,
        )
        # Maya was booked before the customer asked; she can see the customer.
        booking = Booking.objects.get(customer=self.customer)
        Booking.objects.filter(pk=booking.pk).update(staff=self.maya)
        self.client.force_login(self.provider_user)
        page = self.client.get(f"/app/customers/{self.customer.pk}/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Providers to avoid")
        self.assertNotContains(page, "Asked at desk")
        panel = self.client.get(f"/app/appointments/{booking.pk}/", **HTMX)
        self.assertNotContains(panel, "asked not to be booked")
        url = f"/app/customers/{self.customer.pk}/avoid/"
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_the_appointment_warns_the_front_desk(self):
        booking = create_booking(
            organization=self.org,
            service=self.service,
            staff_profile=self.sam,
            customer=self.customer,
            customer_name=self.customer.name,
            customer_email=self.customer.email,
            start_datetime=self.slot(self.sam),
            notify=False,
        )
        Booking.objects.filter(pk=booking.pk).update(staff=self.maya)
        panel = self.client.get(f"/app/appointments/{booking.pk}/", **HTMX)
        self.assertContains(panel, "asked not to be booked with this provider")
