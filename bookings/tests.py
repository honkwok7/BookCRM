from datetime import timedelta

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from bookings.models import Booking
from bookings.services import create_booking
from organizations.models import Organization
from services.models import Service
from staff.models import StaffProfile
from tests.factories import future, make_bookable

User = get_user_model()


class BookingServiceTests(APITestCase):
    def setUp(self):
        self.org1 = Organization.objects.create(name="Org One", slug="org-one")
        self.org2 = Organization.objects.create(name="Org Two", slug="org-two")
        self.staff_user = User.objects.create_user(email="staff@orgone.com", password="Staff12345!")
        self.staff1 = StaffProfile.objects.create(
            user=self.staff_user, organization=self.org1, is_active=True, is_accepting_bookings=True
        )
        self.staff2 = StaffProfile.objects.create(
            user=self.staff_user, organization=self.org2, is_active=True, is_accepting_bookings=True
        )
        self.service1 = Service.objects.create(
            organization=self.org1,
            name="Consultation",
            slug="consultation",
            description="",
            price=100,
            currency="USD",
            duration_minutes=30,
        )
        self.service2 = Service.objects.create(
            organization=self.org2,
            name="Consultation",
            slug="consultation",
            description="",
            price=100,
            currency="USD",
            duration_minutes=30,
        )
        make_bookable(self.staff1, self.service1)
        make_bookable(self.staff2, self.service2)

    def test_prevent_double_booking_same_slot(self):
        start = future(1)
        create_booking(
            organization=self.org1,
            service=self.service1,
            staff_profile=self.staff1,
            customer_name="A",
            customer_email="a@example.com",
            customer_phone="",
            start_datetime=start,
        )
        with self.assertRaises(ValueError):
            create_booking(
                organization=self.org1,
                service=self.service1,
                staff_profile=self.staff1,
                customer_name="B",
                customer_email="b@example.com",
                customer_phone="",
                start_datetime=start + timedelta(minutes=10),
            )

    def test_tenant_isolation_between_orgs(self):
        start = future(1)
        booking1 = create_booking(
            organization=self.org1,
            service=self.service1,
            staff_profile=self.staff1,
            customer_name="A",
            customer_email="a@example.com",
            customer_phone="",
            start_datetime=start,
        )
        create_booking(
            organization=self.org2,
            service=self.service2,
            staff_profile=self.staff2,
            customer_name="B",
            customer_email="b@example.com",
            customer_phone="",
            start_datetime=start,
        )
        self.assertEqual(Booking.objects.filter(organization=self.org1).count(), 1)
        self.assertEqual(Booking.objects.filter(organization=self.org2).count(), 1)
        self.assertEqual(booking1.organization, self.org1)
