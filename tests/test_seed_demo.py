from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from bookings.models import Booking, Customer
from notifications.models import NotificationLog
from organizations.models import Organization


class SeedDemoTests(TestCase):
    def seed(self):
        call_command("seed_demo", allow_without_debug=True, stdout=StringIO())

    def counts(self):
        return (
            get_user_model().objects.count(),
            Organization.objects.count(),
            Customer.objects.count(),
            Booking.objects.count(),
        )

    def test_idempotent_and_silent(self):
        self.seed()
        first = self.counts()
        self.seed()
        self.assertEqual(self.counts(), first)
        self.assertEqual(NotificationLog.objects.count(), 0)

    def test_demo_world_demonstrates_isolation(self):
        self.seed()
        harmony = Organization.objects.get(slug="harmony-wellness")
        self.assertEqual(Customer.objects.filter(email="alex@example.test").count(), 2)
        client = APIClient()
        client.force_authenticate(get_user_model().objects.get(email="owner@harmony.local"))
        rows = client.get("/api/v1/customers/", {"search": "alex"}).json()["results"]
        self.assertEqual([row["organization"] for row in rows], [str(harmony.pk)])

    def test_every_role_is_present(self):
        self.seed()
        roles = set(
            Organization.objects.get(slug="harmony-wellness").memberships.values_list(
                "role", flat=True
            )
        )
        self.assertEqual(roles, {"owner", "manager", "receptionist", "staff"})


class UserUsernameTests(TestCase):
    def test_users_created_without_manager_get_unique_usernames(self):
        User = get_user_model()
        first, _ = User.objects.get_or_create(email="a@example.test")
        second, _ = User.objects.get_or_create(email="b@example.test")
        self.assertTrue(first.username and second.username)
        self.assertNotEqual(first.username, second.username)


class SeedDemoBookingTests(TestCase):
    def test_every_customer_gets_an_upcoming_appointment_through_the_booking_service(self):
        call_command("seed_demo", allow_without_debug=True, stdout=StringIO())
        harmony = Organization.objects.get(slug="harmony-wellness")
        upcoming = Booking.objects.filter(organization=harmony, status=Booking.Status.CONFIRMED)
        self.assertEqual(
            upcoming.values("customer").distinct().count(),
            Customer.objects.filter(organization=harmony).count(),
        )
        self.assertEqual(set(upcoming.values_list("source", flat=True)), {"reception"})
        self.assertFalse(upcoming.filter(location__isnull=True).exists())
        past = Booking.objects.filter(organization=harmony, source=Booking.Source.IMPORT)
        self.assertTrue(past.exists())

    def test_a_chosen_password_replaces_the_published_ones_and_is_not_printed(self):
        out = StringIO()
        call_command(
            "seed_demo", allow_without_debug=True, password="Partner-Demo-2026", stdout=out
        )
        users = get_user_model().objects.filter(
            email__in=["admin@bookcrm.local", "owner@harmony.local", "alex@example.test"]
        )
        self.assertEqual(len(users), 3)
        for user in users:
            self.assertTrue(user.check_password("Partner-Demo-2026"), user.email)
            self.assertFalse(user.check_password("Demo12345!"), user.email)
        self.assertNotIn("Partner-Demo-2026", out.getvalue())
        self.assertNotIn("Demo12345!", out.getvalue())

    def test_the_demo_has_a_published_intake_form(self):
        from customer_forms.models import FormTemplate

        for _ in range(2):  # idempotent
            call_command("seed_demo", allow_without_debug=True, stdout=StringIO())
        form = FormTemplate.objects.get(name="New client intake")
        version = form.versions.get()
        self.assertFalse(version.is_draft)
        self.assertEqual(version.questions.count(), 7)
        self.assertEqual(
            list(form.services.values_list("name", flat=True)), ["Initial Chiropractic Assessment"]
        )
