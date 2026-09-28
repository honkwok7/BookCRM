from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from appointments.models import Appointment, AppointmentStatus
from specialists.models import Specialist, Weekday, WorkingHour

User = get_user_model()


def next_weekday(weekday: int) -> date:
    """Return the nearest future date that falls on the given weekday (0=Monday)."""
    today = date.today()
    days_ahead = weekday - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


class AppointmentAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="user1@example.com", password="strongpass123")
        self.other_user = User.objects.create_user(
            email="user2@example.com", password="strongpass123"
        )
        self.admin = User.objects.create_superuser(
            email="admin@example.com", password="adminpass123", role=UserRole.ADMIN
        )
        self.specialist = Specialist.objects.create(name="Dr. House", profession="General")
        WorkingHour.objects.create(
            specialist=self.specialist,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        self.target_date = next_weekday(Weekday.MONDAY)

    def test_appointment_creation(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            "/api/appointments/",
            {
                "specialist": self.specialist.id,
                "date": self.target_date,
                "time": "10:00:00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_duplicate_appointment_prevention(self):
        Appointment.objects.create(
            user=self.user,
            specialist=self.specialist,
            date=self.target_date,
            time="10:00:00",
            status=AppointmentStatus.PENDING,
        )
        self.client.force_authenticate(user=self.other_user)
        response = self.client.post(
            "/api/appointments/",
            {
                "specialist": self.specialist.id,
                "date": self.target_date,
                "time": "10:00:00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_confirm_appointment(self):
        appointment = Appointment.objects.create(
            user=self.user,
            specialist=self.specialist,
            date=self.target_date,
            time="11:00:00",
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.patch(f"/api/appointments/{appointment.id}/confirm/", format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_confirm_appointment(self):
        appointment = Appointment.objects.create(
            user=self.user,
            specialist=self.specialist,
            date=self.target_date,
            time="12:00:00",
        )
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appointment.id}/confirm/", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    def test_past_date_booking_rejected(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist.id, "date": "2020-01-06", "time": "10:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_out_of_working_hours_rejected(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist.id, "date": self.target_date, "time": "08:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancel_by_owner(self):
        appointment = Appointment.objects.create(
            user=self.user,
            specialist=self.specialist,
            date=self.target_date,
            time="10:00:00",
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.patch(f"/api/appointments/{appointment.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cancel_by_non_owner_returns_403(self):
        appointment = Appointment.objects.create(
            user=self.user,
            specialist=self.specialist,
            date=self.target_date,
            time="10:00:00",
        )
        self.client.force_authenticate(user=self.other_user)
        response = self.client.patch(f"/api/appointments/{appointment.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------------ #
    # Slot-alignment validation                                            #
    # ------------------------------------------------------------------ #

    def test_aligned_time_accepted(self):
        """10:30 aligns with 30-min grid starting at 09:00."""
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist.id, "date": self.target_date, "time": "10:30:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_unaligned_time_rejected(self):
        """10:15 does not align with 30-min grid starting at 09:00."""
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist.id, "date": self.target_date, "time": "10:15:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_alignment_with_60min_slot_duration(self):
        self.specialist.slot_duration = 60
        self.specialist.save()
        self.client.force_authenticate(user=self.user)
        # 10:30 is NOT aligned to 60-min grid from 09:00 (09:00, 10:00, 11:00…)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist.id, "date": self.target_date, "time": "10:30:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AppointmentFieldTests(APITestCase):
    """Tests for the notes, duration, and updated_at fields."""

    def setUp(self):
        self.user = User.objects.create_user("user1", password="strongpass123")
        self.admin = User.objects.create_superuser(
            email="admin@example.com", password="adminpass123", role=UserRole.ADMIN
        )
        self.specialist = Specialist.objects.create(
            name="Dr. Field", profession="General", slot_duration=30
        )
        WorkingHour.objects.create(
            specialist=self.specialist,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        self.target_date = next_weekday(Weekday.MONDAY)

    def _create_appointment(self, t="10:00:00", notes="", duration=None):
        payload = {"specialist": self.specialist.id, "date": self.target_date, "time": t}
        if notes:
            payload["notes"] = notes
        if duration is not None:
            payload["duration"] = duration
        self.client.force_authenticate(user=self.user)
        return self.client.post("/api/appointments/", payload, format="json")

    # ------------------------------------------------------------------ #
    # notes                                                                #
    # ------------------------------------------------------------------ #

    def test_notes_default_to_empty_string(self):
        response = self._create_appointment()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["notes"], "")

    def test_notes_can_be_set_on_creation(self):
        response = self._create_appointment(notes="First visit")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["notes"], "First visit")

    def test_notes_persisted_to_db(self):
        self._create_appointment(notes="Follow-up")
        appt = Appointment.objects.get()
        self.assertEqual(appt.notes, "Follow-up")

    def test_notes_exposed_in_list_view(self):
        Appointment.objects.create(
            user=self.user,
            specialist=self.specialist,
            date=self.target_date,
            time=time(10, 0),
            notes="Important",
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/my-appointments/")
        self.assertEqual(response.data["results"][0]["notes"], "Important")

    # ------------------------------------------------------------------ #
    # duration                                                             #
    # ------------------------------------------------------------------ #

    def test_duration_defaults_to_specialist_slot_duration(self):
        response = self._create_appointment()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["duration"], self.specialist.slot_duration)

    def test_duration_can_be_overridden_on_creation(self):
        response = self._create_appointment(duration=60)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["duration"], 60)

    def test_duration_persisted_to_db(self):
        self._create_appointment(duration=45)
        appt = Appointment.objects.get()
        self.assertEqual(appt.duration, 45)

    def test_duration_is_exposed_in_api(self):
        self._create_appointment()
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/my-appointments/")
        self.assertIn("duration", response.data["results"][0])

    # ------------------------------------------------------------------ #
    # updated_at                                                           #
    # ------------------------------------------------------------------ #

    def test_updated_at_set_on_creation(self):
        response = self._create_appointment()
        self.assertIsNotNone(response.data["updated_at"])

    def test_updated_at_changes_when_status_changes(self):
        appt = Appointment.objects.create(
            user=self.user,
            specialist=self.specialist,
            date=self.target_date,
            time=time(10, 0),
        )
        original_updated_at = appt.updated_at
        self.client.force_authenticate(user=self.admin)
        self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        appt.refresh_from_db()
        self.assertGreaterEqual(appt.updated_at, original_updated_at)

    def test_updated_at_is_read_only_in_api(self):
        """Clients cannot manually set updated_at."""
        payload = {
            "specialist": self.specialist.id,
            "date": self.target_date,
            "time": "10:00:00",
            "updated_at": "2000-01-01T00:00:00Z",
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.post("/api/appointments/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # The returned updated_at must NOT be the supplied value
        self.assertNotEqual(response.data["updated_at"], "2000-01-01T00:00:00Z")


class AppointmentLifecycleTests(APITestCase):
    """Tests for valid and invalid status transitions."""

    def setUp(self):
        self.user = User.objects.create_user("user1", password="strongpass123")
        self.admin = User.objects.create_superuser(
            email="admin@example.com", password="adminpass123", role=UserRole.ADMIN
        )
        self.specialist = Specialist.objects.create(name="Dr. Lifecycle", profession="General")
        WorkingHour.objects.create(
            specialist=self.specialist,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        self.monday = next_weekday(Weekday.MONDAY)

    def _make_appointment(self, appt_status=AppointmentStatus.PENDING, t="10:00:00"):
        return Appointment.objects.create(
            user=self.user,
            specialist=self.specialist,
            date=self.monday,
            time=t,
            status=appt_status,
        )

    # ------------------------------------------------------------------ #
    # VALID transitions                                                    #
    # ------------------------------------------------------------------ #

    def test_pending_to_confirmed(self):
        appt = self._make_appointment(AppointmentStatus.PENDING)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        appt.refresh_from_db()
        self.assertEqual(appt.status, AppointmentStatus.CONFIRMED)

    def test_pending_to_cancelled(self):
        appt = self._make_appointment(AppointmentStatus.PENDING)
        self.client.force_authenticate(user=self.user)
        response = self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        appt.refresh_from_db()
        self.assertEqual(appt.status, AppointmentStatus.CANCELLED)

    def test_confirmed_to_completed(self):
        appt = self._make_appointment(AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        appt.refresh_from_db()
        self.assertEqual(appt.status, AppointmentStatus.COMPLETED)

    def test_confirmed_to_no_show(self):
        appt = self._make_appointment(AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        appt.refresh_from_db()
        self.assertEqual(appt.status, AppointmentStatus.NO_SHOW)

    def test_confirmed_to_cancelled(self):
        appt = self._make_appointment(AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        appt.refresh_from_db()
        self.assertEqual(appt.status, AppointmentStatus.CANCELLED)

    # ------------------------------------------------------------------ #
    # INVALID transitions — PENDING source                                #
    # ------------------------------------------------------------------ #

    def test_pending_to_completed_rejected(self):
        appt = self._make_appointment(AppointmentStatus.PENDING)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        appt.refresh_from_db()
        self.assertEqual(appt.status, AppointmentStatus.PENDING)

    def test_pending_to_no_show_rejected(self):
        appt = self._make_appointment(AppointmentStatus.PENDING)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        appt.refresh_from_db()
        self.assertEqual(appt.status, AppointmentStatus.PENDING)

    # ------------------------------------------------------------------ #
    # INVALID transitions — COMPLETED source (terminal)                   #
    # ------------------------------------------------------------------ #

    def test_completed_to_confirmed_rejected(self):
        appt = self._make_appointment(AppointmentStatus.COMPLETED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_completed_to_cancelled_rejected(self):
        appt = self._make_appointment(AppointmentStatus.COMPLETED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_completed_to_no_show_rejected(self):
        appt = self._make_appointment(AppointmentStatus.COMPLETED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_completed_to_completed_rejected(self):
        appt = self._make_appointment(AppointmentStatus.COMPLETED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ------------------------------------------------------------------ #
    # INVALID transitions — NO_SHOW source (terminal)                     #
    # ------------------------------------------------------------------ #

    def test_no_show_to_confirmed_rejected(self):
        appt = self._make_appointment(AppointmentStatus.NO_SHOW)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_show_to_completed_rejected(self):
        appt = self._make_appointment(AppointmentStatus.NO_SHOW)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_show_to_cancelled_rejected(self):
        appt = self._make_appointment(AppointmentStatus.NO_SHOW)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_show_to_no_show_rejected(self):
        appt = self._make_appointment(AppointmentStatus.NO_SHOW)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ------------------------------------------------------------------ #
    # INVALID transitions — CANCELLED source (terminal)                   #
    # ------------------------------------------------------------------ #

    def test_cancelled_to_confirmed_rejected(self):
        appt = self._make_appointment(AppointmentStatus.CANCELLED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancelled_to_completed_rejected(self):
        appt = self._make_appointment(AppointmentStatus.CANCELLED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancelled_to_no_show_rejected(self):
        appt = self._make_appointment(AppointmentStatus.CANCELLED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancelled_to_cancelled_rejected(self):
        appt = self._make_appointment(AppointmentStatus.CANCELLED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ------------------------------------------------------------------ #
    # DB state & response payload                                          #
    # ------------------------------------------------------------------ #

    def test_complete_response_payload(self):
        appt = self._make_appointment(AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertIn("detail", response.data)

    def test_no_show_response_payload(self):
        appt = self._make_appointment(AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertIn("detail", response.data)

    def test_invalid_transition_error_message_contains_statuses(self):
        appt = self._make_appointment(AppointmentStatus.PENDING)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Error must mention both statuses so the client knows what happened
        error_text = str(response.data)
        self.assertIn("pending", error_text)
        self.assertIn("completed", error_text)

    def test_complete_requires_admin(self):
        appt = self._make_appointment(AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.user)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_show_requires_admin(self):
        appt = self._make_appointment(AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.user)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------------ #
    # Slot availability regression                                         #
    # ------------------------------------------------------------------ #

    def test_cancelled_appointment_releases_slot(self):
        """After cancellation the slot must appear in available-slots again."""
        appt = self._make_appointment(AppointmentStatus.PENDING, t="09:00:00")
        self.client.force_authenticate(user=self.user)
        self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        response = self.client.get(
            f"/api/specialists/{self.specialist.id}/available-slots/",
            {"date": self.monday},
        )
        self.assertIn("09:00", response.data["slots"])

    def test_confirmed_appointment_blocks_slot(self):
        """A confirmed appointment must not appear in available-slots."""
        self._make_appointment(AppointmentStatus.CONFIRMED, t="09:00:00")
        response = self.client.get(
            f"/api/specialists/{self.specialist.id}/available-slots/",
            {"date": self.monday},
        )
        self.assertNotIn("09:00", response.data["slots"])

    def test_completed_appointment_releases_slot(self):
        """A completed appointment is no longer PENDING/CONFIRMED, so slot is free."""
        self._make_appointment(AppointmentStatus.COMPLETED, t="09:00:00")
        response = self.client.get(
            f"/api/specialists/{self.specialist.id}/available-slots/",
            {"date": self.monday},
        )
        self.assertIn("09:00", response.data["slots"])

    def test_no_show_appointment_releases_slot(self):
        self._make_appointment(AppointmentStatus.NO_SHOW, t="09:00:00")
        response = self.client.get(
            f"/api/specialists/{self.specialist.id}/available-slots/",
            {"date": self.monday},
        )
        self.assertIn("09:00", response.data["slots"])


# ======================================================================== #
# Step 4 — Roles & Permissions tests                                        #
# ======================================================================== #

from accounts.models import UserRole  # noqa: E402  (import after existing test classes)


def make_specialist_user(username, specialist):
    """Create a user with the specialist role and link them to a Specialist profile."""
    user = User.objects.create_user(username, password="strongpass123", role=UserRole.SPECIALIST)
    specialist.user = user
    specialist.save()
    return user


class CustomerPermissionTests(APITestCase):
    """Customers can create/view/cancel their own appointments; nothing else."""

    def setUp(self):
        self.customer = User.objects.create_user(
            "customer", password="pass123", role=UserRole.CUSTOMER
        )
        self.other_customer = User.objects.create_user(
            "other", password="pass123", role=UserRole.CUSTOMER
        )
        self.admin = User.objects.create_superuser(
            email="a@example.com", password="pass123", role=UserRole.ADMIN
        )
        self.specialist = Specialist.objects.create(name="Dr. K", profession="GP")
        WorkingHour.objects.create(
            specialist=self.specialist,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        self.monday = next_weekday(Weekday.MONDAY)

    def _appt(self, user, t="10:00:00", appt_status=AppointmentStatus.PENDING):
        return Appointment.objects.create(
            user=user,
            specialist=self.specialist,
            date=self.monday,
            time=t,
            status=appt_status,
        )

    # ------------------------------------------------------------------ #
    # Create / view own                                                    #
    # ------------------------------------------------------------------ #

    def test_customer_can_create_appointment(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist.id, "date": self.monday, "time": "10:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_customer_can_view_own_appointments(self):
        self._appt(self.customer)
        self.client.force_authenticate(user=self.customer)
        response = self.client.get("/api/my-appointments/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_customer_cannot_see_other_customers_appointments(self):
        self._appt(self.other_customer)
        self.client.force_authenticate(user=self.customer)
        response = self.client.get("/api/my-appointments/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 0)

    def test_customer_can_cancel_own_appointment(self):
        appt = self._appt(self.customer)
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # ------------------------------------------------------------------ #
    # Forbidden lifecycle actions                                          #
    # ------------------------------------------------------------------ #

    def test_customer_cannot_confirm_appointment(self):
        appt = self._appt(self.customer)
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_cannot_complete_appointment(self):
        appt = self._appt(self.customer, appt_status=AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_cannot_mark_no_show(self):
        appt = self._appt(self.customer, appt_status=AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_cannot_access_admin_appointment_list(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.get("/api/appointments/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------------ #
    # Direct-ID authorization bypass attempts                             #
    # ------------------------------------------------------------------ #

    def test_customer_cannot_cancel_another_customers_appointment(self):
        appt = self._appt(self.other_customer, t="10:00:00")
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_cannot_confirm_another_customers_appointment(self):
        appt = self._appt(self.other_customer)
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------------ #
    # Specialist management endpoints forbidden for customers             #
    # ------------------------------------------------------------------ #

    def test_customer_cannot_create_specialist(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.post(
            "/api/specialists/",
            {"name": "Dr. X", "profession": "GP"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_cannot_add_working_hours(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.post(
            f"/api/specialists/{self.specialist.id}/working-hours/",
            {"day": Weekday.MONDAY, "start_time": "09:00", "end_time": "12:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SpecialistPermissionTests(APITestCase):
    """Specialists can act on their assigned appointments; not on others'."""

    def setUp(self):
        self.customer = User.objects.create_user("customer", password="pass123")
        self.admin = User.objects.create_superuser(
            email="a@example.com", password="pass123", role=UserRole.ADMIN
        )

        # Two specialists with linked user accounts
        self.specialist_a = Specialist.objects.create(name="Dr. A", profession="GP")
        self.specialist_b = Specialist.objects.create(name="Dr. B", profession="Dentist")
        self.spec_user_a = make_specialist_user("spec_a", self.specialist_a)
        self.spec_user_b = make_specialist_user("spec_b", self.specialist_b)

        WorkingHour.objects.create(
            specialist=self.specialist_a,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        WorkingHour.objects.create(
            specialist=self.specialist_b,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        self.monday = next_weekday(Weekday.MONDAY)

    def _appt(self, specialist, t="10:00:00", appt_status=AppointmentStatus.PENDING):
        return Appointment.objects.create(
            user=self.customer,
            specialist=specialist,
            date=self.monday,
            time=t,
            status=appt_status,
        )

    # ------------------------------------------------------------------ #
    # View own appointments                                                #
    # ------------------------------------------------------------------ #

    def test_specialist_can_view_own_assigned_appointments(self):
        self._appt(self.specialist_a)
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.get(f"/api/specialists/{self.specialist_a.id}/appointments/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_specialist_cannot_view_another_specialists_appointments(self):
        self._appt(self.specialist_b)
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.get(f"/api/specialists/{self.specialist_b.id}/appointments/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------------ #
    # Valid lifecycle actions on own appointments                          #
    # ------------------------------------------------------------------ #

    def test_specialist_can_confirm_own_appointment(self):
        appt = self._appt(self.specialist_a)
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_specialist_can_complete_own_appointment(self):
        appt = self._appt(self.specialist_a, appt_status=AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_specialist_can_mark_own_appointment_no_show(self):
        appt = self._appt(self.specialist_a, appt_status=AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_specialist_can_cancel_own_appointment(self):
        appt = self._appt(self.specialist_a)
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # ------------------------------------------------------------------ #
    # Direct-ID bypass: specialist vs another specialist's appointment     #
    # ------------------------------------------------------------------ #

    def test_specialist_cannot_confirm_another_specialists_appointment(self):
        appt = self._appt(self.specialist_b)
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_specialist_cannot_complete_another_specialists_appointment(self):
        appt = self._appt(self.specialist_b, appt_status=AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_specialist_cannot_no_show_another_specialists_appointment(self):
        appt = self._appt(self.specialist_b, appt_status=AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_specialist_cannot_cancel_another_specialists_appointment(self):
        appt = self._appt(self.specialist_b)
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------------ #
    # Specialist cannot access admin-only endpoints                        #
    # ------------------------------------------------------------------ #

    def test_specialist_cannot_access_admin_appointment_list(self):
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.get("/api/appointments/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_specialist_cannot_create_another_specialist(self):
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.post(
            "/api/specialists/",
            {"name": "Dr. New", "profession": "GP"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_specialist_cannot_add_working_hours_to_another_specialist(self):
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.post(
            f"/api/specialists/{self.specialist_b.id}/working-hours/",
            {"day": Weekday.TUESDAY, "start_time": "09:00", "end_time": "12:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------------ #
    # Specialist must NOT create appointments via POST /api/appointments/  #
    # ------------------------------------------------------------------ #

    def test_specialist_cannot_create_appointment_for_self(self):
        """Specialist booking an appointment as a customer must be rejected."""
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist_a.id, "date": self.monday, "time": "10:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_specialist_cannot_create_appointment_for_another_customer(self):
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist_a.id, "date": self.monday, "time": "11:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_specialist_cannot_create_appointment_assigned_to_themselves(self):
        """Even with specialist=self.specialist_a in payload, must be rejected."""
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist_a.id, "date": self.monday, "time": "12:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_specialist_cannot_create_appointment_assigned_to_another_specialist(self):
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist_b.id, "date": self.monday, "time": "10:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AppointmentCreationAuthorizationTests(APITestCase):
    """Customer booking succeeds; administrative roles cannot use customer booking."""

    def setUp(self):
        self.customer_a = User.objects.create_user("cust_a", password="pass123")
        self.customer_b = User.objects.create_user("cust_b", password="pass123")
        self.admin = User.objects.create_superuser(
            email="a@example.com", password="pass123", role=UserRole.ADMIN
        )
        self.specialist = Specialist.objects.create(name="Dr. R", profession="GP")
        WorkingHour.objects.create(
            specialist=self.specialist,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        self.monday = next_weekday(Weekday.MONDAY)

    def test_customer_can_create_own_appointment(self):
        self.client.force_authenticate(user=self.customer_a)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist.id, "date": self.monday, "time": "10:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Appointment.objects.get().user, self.customer_a)

    def test_admin_cannot_create_customer_appointment(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist.id, "date": self.monday, "time": "10:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Appointment.objects.exists())


class AdminPermissionTests(APITestCase):
    """Admin roles have full appointment management access."""

    def setUp(self):
        self.customer = User.objects.create_user("customer", password="pass123")
        self.admin = User.objects.create_superuser(
            email="a@example.com", password="pass123", role=UserRole.ADMIN
        )
        self.specialist = Specialist.objects.create(name="Dr. K", profession="GP")
        WorkingHour.objects.create(
            specialist=self.specialist,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        self.monday = next_weekday(Weekday.MONDAY)

    def _appt(self, t="10:00:00", appt_status=AppointmentStatus.PENDING):
        return Appointment.objects.create(
            user=self.customer,
            specialist=self.specialist,
            date=self.monday,
            time=t,
            status=appt_status,
        )

    def test_admin_can_view_all_appointments(self):
        self._appt("10:00:00")
        self._appt("11:00:00")
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/appointments/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)

    def test_admin_list_shows_all_customers_appointments(self):
        other = User.objects.create_user("other", password="pass123")
        self._appt("10:00:00")
        Appointment.objects.create(
            user=other,
            specialist=self.specialist,
            date=self.monday,
            time=time(11, 0),
        )
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/appointments/")
        self.assertEqual(len(response.data["results"]), 2)

    def test_admin_can_confirm(self):
        appt = self._appt()
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_admin_can_cancel(self):
        appt = self._appt()
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_admin_can_complete(self):
        appt = self._appt(appt_status=AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_admin_can_mark_no_show(self):
        appt = self._appt(appt_status=AppointmentStatus.CONFIRMED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/no-show/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_admin_can_view_specialist_appointments(self):
        self._appt()
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/specialists/{self.specialist.id}/appointments/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_admin_can_create_specialist(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/specialists/",
            {"name": "Dr. New", "profession": "GP"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_admin_can_delete_working_hour(self):
        wh = WorkingHour.objects.get(specialist=self.specialist)
        self.client.force_authenticate(user=self.admin)
        response = self.client.delete(
            f"/api/specialists/{self.specialist.id}/working-hours/{wh.id}/"
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


class UnauthenticatedPermissionTests(APITestCase):
    """Unauthenticated requests must be rejected on protected endpoints."""

    def setUp(self):
        self.customer = User.objects.create_user("customer", password="pass123")
        self.specialist = Specialist.objects.create(name="Dr. K", profession="GP")
        WorkingHour.objects.create(
            specialist=self.specialist,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        self.monday = next_weekday(Weekday.MONDAY)
        self.appt = Appointment.objects.create(
            user=self.customer,
            specialist=self.specialist,
            date=self.monday,
            time=time(10, 0),
        )

    def test_unauthenticated_cannot_create_appointment(self):
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist.id, "date": self.monday, "time": "11:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_cannot_view_my_appointments(self):
        response = self.client.get("/api/my-appointments/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_cannot_access_admin_list(self):
        response = self.client.get("/api/appointments/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_cannot_cancel(self):
        response = self.client.patch(f"/api/appointments/{self.appt.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_cannot_confirm(self):
        response = self.client.patch(f"/api/appointments/{self.appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_cannot_complete(self):
        appt = Appointment.objects.create(
            user=self.customer,
            specialist=self.specialist,
            date=self.monday,
            time=time(11, 0),
            status=AppointmentStatus.CONFIRMED,
        )
        response = self.client.patch(f"/api/appointments/{appt.id}/complete/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_cannot_view_specialist_appointments(self):
        response = self.client.get(f"/api/specialists/{self.specialist.id}/appointments/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class RoleFieldTests(APITestCase):
    """Tests for the role field on the User model."""

    def test_default_role_is_customer(self):
        user = User.objects.create_user("u", password="pass123")
        self.assertEqual(user.role, UserRole.CUSTOMER)

    def test_specialist_role_can_be_set(self):
        user = User.objects.create_user("s", password="pass123", role=UserRole.SPECIALIST)
        self.assertEqual(user.role, UserRole.SPECIALIST)

    def test_admin_role_can_be_set(self):
        user = User.objects.create_user("a", password="pass123", role=UserRole.ADMIN)
        self.assertEqual(user.role, UserRole.ADMIN)

    def test_owner_role_can_be_set(self):
        user = User.objects.create_user("o", password="pass123", role=UserRole.OWNER)
        self.assertEqual(user.role, UserRole.OWNER)

    def test_specialist_profile_link(self):
        specialist = Specialist.objects.create(name="Dr. Z", profession="GP")
        user = make_specialist_user("linked", specialist)
        self.assertEqual(user.specialist_profile, specialist)
        self.assertEqual(specialist.user, user)

    def test_unlinked_user_has_no_specialist_profile(self):
        user = User.objects.create_user("plain", password="pass123")
        with self.assertRaises(User.specialist_profile.RelatedObjectDoesNotExist):
            _ = user.specialist_profile


# ======================================================================== #
# Phase 1 — Step 5: Appointment Reschedule tests                           #
# ======================================================================== #


class AppointmentRescheduleTests(APITestCase):
    """Comprehensive tests for PATCH /api/appointments/<pk>/reschedule/"""

    def setUp(self):
        self.customer = User.objects.create_user("customer", password="pass123")
        self.other_customer = User.objects.create_user("other_customer", password="pass123")
        self.admin = User.objects.create_superuser(
            email="a@example.com", password="pass123", role=UserRole.ADMIN
        )

        self.specialist_a = Specialist.objects.create(
            name="Dr. A", profession="GP", slot_duration=30
        )
        self.specialist_b = Specialist.objects.create(
            name="Dr. B", profession="Dentist", slot_duration=30
        )
        self.spec_user_a = make_specialist_user("spec_a", self.specialist_a)
        self.spec_user_b = make_specialist_user("spec_b", self.specialist_b)

        WorkingHour.objects.create(
            specialist=self.specialist_a,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        WorkingHour.objects.create(
            specialist=self.specialist_b,
            day=Weekday.MONDAY,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )

        self.monday = next_weekday(Weekday.MONDAY)
        self.next_monday = self.monday + timedelta(weeks=1)

    def _appt(
        self, user=None, specialist=None, t="10:00:00", appt_status=AppointmentStatus.PENDING
    ):
        return Appointment.objects.create(
            user=user or self.customer,
            specialist=specialist or self.specialist_a,
            date=self.monday,
            time=t,
            status=appt_status,
        )

    def _reschedule(self, pk, new_date, new_time, auth_user):
        self.client.force_authenticate(user=auth_user)
        return self.client.patch(
            f"/api/appointments/{pk}/reschedule/",
            {"date": str(new_date), "time": new_time},
            format="json",
        )

    # ------------------------------------------------------------------ #
    # Successful reschedule                                                #
    # ------------------------------------------------------------------ #

    def test_customer_reschedules_own_pending_appointment(self):
        appt = self._appt(appt_status=AppointmentStatus.PENDING)
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        appt.refresh_from_db()
        self.assertEqual(appt.date, self.next_monday)
        self.assertEqual(appt.time, time(11, 0))

    def test_customer_reschedules_own_confirmed_appointment(self):
        appt = self._appt(appt_status=AppointmentStatus.CONFIRMED)
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        appt.refresh_from_db()
        self.assertEqual(appt.date, self.next_monday)

    def test_specialist_reschedules_assigned_appointment(self):
        appt = self._appt(specialist=self.specialist_a)
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.spec_user_a)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_admin_reschedules_appointment(self):
        appt = self._appt()
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # ------------------------------------------------------------------ #
    # Data preservation                                                    #
    # ------------------------------------------------------------------ #

    def test_appointment_id_unchanged(self):
        appt = self._appt()
        original_id = appt.id
        self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        appt.refresh_from_db()
        self.assertEqual(appt.id, original_id)

    def test_customer_field_unchanged(self):
        appt = self._appt()
        self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        appt.refresh_from_db()
        self.assertEqual(appt.user, self.customer)

    def test_specialist_field_unchanged(self):
        appt = self._appt(specialist=self.specialist_a)
        self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        appt.refresh_from_db()
        self.assertEqual(appt.specialist, self.specialist_a)

    def test_notes_preserved(self):
        appt = self._appt()
        appt.notes = "Important notes"
        appt.save()
        self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        appt.refresh_from_db()
        self.assertEqual(appt.notes, "Important notes")

    def test_duration_preserved(self):
        appt = self._appt()
        appt.duration = 45
        appt.save()
        self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        appt.refresh_from_db()
        self.assertEqual(appt.duration, 45)

    def test_created_at_preserved(self):
        appt = self._appt()
        original_created_at = appt.created_at
        self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        appt.refresh_from_db()
        self.assertEqual(appt.created_at, original_created_at)

    def test_status_unchanged_after_reschedule(self):
        appt = self._appt(appt_status=AppointmentStatus.PENDING)
        self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        appt.refresh_from_db()
        self.assertEqual(appt.status, AppointmentStatus.PENDING)

    def test_updated_at_changes_after_reschedule(self):
        appt = self._appt()
        original_updated_at = appt.updated_at
        self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        appt.refresh_from_db()
        self.assertGreaterEqual(appt.updated_at, original_updated_at)

    def test_old_slot_becomes_available_after_reschedule(self):
        appt = self._appt(t="09:00:00")
        self._reschedule(appt.id, self.next_monday, "10:00:00", self.customer)
        response = self.client.get(
            f"/api/specialists/{self.specialist_a.id}/available-slots/",
            {"date": self.monday},
        )
        self.assertIn("09:00", response.data["slots"])

    def test_new_slot_becomes_occupied_after_reschedule(self):
        appt = self._appt(t="09:00:00")
        self._reschedule(appt.id, self.next_monday, "10:00:00", self.customer)
        response = self.client.get(
            f"/api/specialists/{self.specialist_a.id}/available-slots/",
            {"date": self.next_monday},
        )
        self.assertNotIn("10:00", response.data["slots"])

    def test_response_contains_updated_datetime(self):
        appt = self._appt()
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.assertEqual(str(response.data["date"]), str(self.next_monday))
        self.assertIn("11:00", response.data["time"])

    def test_response_uses_appointment_serializer_fields(self):
        appt = self._appt()
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        for field in ("id", "user", "specialist", "date", "time", "status", "duration", "notes"):
            self.assertIn(field, response.data)

    # ------------------------------------------------------------------ #
    # Permission tests                                                     #
    # ------------------------------------------------------------------ #

    def test_unauthenticated_rejected_with_401(self):
        appt = self._appt()
        response = self.client.patch(
            f"/api/appointments/{appt.id}/reschedule/",
            {"date": str(self.next_monday), "time": "11:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_other_customer_rejected_with_403(self):
        appt = self._appt(user=self.customer)
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.other_customer)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_specialist_b_cannot_reschedule_specialist_a_appointment(self):
        appt = self._appt(specialist=self.specialist_a)
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.spec_user_b)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_other_customer_appointment_unchanged_after_rejected_reschedule(self):
        appt = self._appt(user=self.customer)
        self._reschedule(appt.id, self.next_monday, "11:00:00", self.other_customer)
        appt.refresh_from_db()
        self.assertEqual(appt.date, self.monday)

    # ------------------------------------------------------------------ #
    # Status restriction tests                                             #
    # ------------------------------------------------------------------ #

    def test_pending_can_be_rescheduled(self):
        appt = self._appt(appt_status=AppointmentStatus.PENDING)
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_confirmed_can_be_rescheduled(self):
        appt = self._appt(appt_status=AppointmentStatus.CONFIRMED)
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cancelled_cannot_be_rescheduled(self):
        appt = self._appt(appt_status=AppointmentStatus.CANCELLED)
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        appt.refresh_from_db()
        self.assertEqual(appt.date, self.monday)

    def test_completed_cannot_be_rescheduled(self):
        appt = self._appt(appt_status=AppointmentStatus.COMPLETED)
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        appt.refresh_from_db()
        self.assertEqual(appt.date, self.monday)

    def test_no_show_cannot_be_rescheduled(self):
        appt = self._appt(appt_status=AppointmentStatus.NO_SHOW)
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        appt.refresh_from_db()
        self.assertEqual(appt.date, self.monday)

    # ------------------------------------------------------------------ #
    # Slot / working-hour validation tests                                 #
    # ------------------------------------------------------------------ #

    def test_past_date_rejected(self):
        appt = self._appt()
        response = self._reschedule(appt.id, date(2020, 1, 6), "10:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        appt.refresh_from_db()
        self.assertEqual(appt.date, self.monday)

    def test_outside_working_hours_rejected(self):
        appt = self._appt()
        response = self._reschedule(appt.id, self.next_monday, "08:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unaligned_slot_rejected(self):
        appt = self._appt()
        response = self._reschedule(appt.id, self.next_monday, "10:15:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_occupied_slot_rejected(self):
        appt = self._appt(t="10:00:00")
        Appointment.objects.create(
            user=self.other_customer,
            specialist=self.specialist_a,
            date=self.next_monday,
            time=time(11, 0),
            status=AppointmentStatus.PENDING,
        )
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duration_must_fit_inside_working_hours(self):
        appt = self._appt(t="10:00:00")
        appt.duration = 60
        appt.save()
        response = self._reschedule(appt.id, self.next_monday, "16:30:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        appt.refresh_from_db()
        self.assertEqual(appt.date, self.monday)

    def test_partial_overlap_at_end_rejected(self):
        appt = self._appt(t="09:00:00")
        Appointment.objects.create(
            user=self.other_customer,
            specialist=self.specialist_a,
            date=self.next_monday,
            time=time(10, 45),
            duration=30,
        )
        response = self._reschedule(appt.id, self.next_monday, "10:30:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rescheduled_interval_containing_existing_appointment_rejected(self):
        appt = self._appt(t="09:00:00")
        appt.duration = 90
        appt.save()
        Appointment.objects.create(
            user=self.other_customer,
            specialist=self.specialist_a,
            date=self.next_monday,
            time=time(10, 30),
            duration=30,
        )
        response = self._reschedule(appt.id, self.next_monday, "10:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_existing_interval_containing_rescheduled_appointment_rejected(self):
        appt = self._appt(t="09:00:00")
        Appointment.objects.create(
            user=self.other_customer,
            specialist=self.specialist_a,
            date=self.next_monday,
            time=time(10, 0),
            duration=120,
        )
        response = self._reschedule(appt.id, self.next_monday, "10:30:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_same_slot_rejected(self):
        appt = self._appt(t="10:00:00")
        response = self._reschedule(appt.id, self.monday, "10:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ------------------------------------------------------------------ #
    # Self-overlap exclusion (Step 9)                                      #
    # ------------------------------------------------------------------ #

    def test_reschedule_to_free_slot_not_blocked_by_self(self):
        """Appointment at 10:00 must not block reschedule to 11:00."""
        appt = self._appt(t="10:00:00")
        response = self._reschedule(appt.id, self.monday, "11:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cancelled_appointment_at_new_slot_does_not_block(self):
        """CANCELLED appointment at the target slot must not prevent rescheduling."""
        appt = self._appt(t="10:00:00")
        Appointment.objects.create(
            user=self.other_customer,
            specialist=self.specialist_a,
            date=self.next_monday,
            time=time(11, 0),
            status=AppointmentStatus.CANCELLED,
        )
        response = self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # ------------------------------------------------------------------ #
    # Atomicity (Step 10)                                                  #
    # ------------------------------------------------------------------ #

    def test_failed_reschedule_leaves_all_fields_unchanged(self):
        appt = self._appt(t="10:00:00")
        appt.notes = "Keep this"
        appt.duration = 45
        appt.save()
        appt.refresh_from_db()  # normalize in-memory string fields to Python types
        original_date = appt.date
        original_time = appt.time
        original_notes = appt.notes
        original_duration = appt.duration
        original_status = appt.status

        self._reschedule(appt.id, date(2020, 1, 6), "10:00:00", self.customer)
        appt.refresh_from_db()

        self.assertEqual(appt.date, original_date)
        self.assertEqual(appt.time, original_time)
        self.assertEqual(appt.notes, original_notes)
        self.assertEqual(appt.duration, original_duration)
        self.assertEqual(appt.status, original_status)

    # ------------------------------------------------------------------ #
    # Boundary tests (Step 12)                                             #
    # ------------------------------------------------------------------ #

    def test_reschedule_to_exact_working_hour_start(self):
        appt = self._appt(t="10:00:00")
        response = self._reschedule(appt.id, self.next_monday, "09:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_reschedule_to_last_valid_slot(self):
        """16:30 is the last 30-min slot ending exactly at 17:00."""
        appt = self._appt(t="10:00:00")
        response = self._reschedule(appt.id, self.next_monday, "16:30:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_reschedule_to_end_of_working_hours_rejected(self):
        """17:00 is boundary-end; a 30-min slot starting there exceeds working hours."""
        appt = self._appt(t="10:00:00")
        response = self._reschedule(appt.id, self.next_monday, "17:00:00", self.customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_back_to_back_appointments_allowed(self):
        """A slot adjacent to an existing appointment must be bookable."""
        self._appt(t="09:00:00")
        appt_b = self._appt(user=self.other_customer, t="11:00:00")
        response = self._reschedule(appt_b.id, self.monday, "09:30:00", self.other_customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # ------------------------------------------------------------------ #
    # Regression tests (Step 17)                                           #
    # ------------------------------------------------------------------ #

    def test_available_slots_still_works_after_reschedule(self):
        appt = self._appt(t="09:00:00")
        self._reschedule(appt.id, self.next_monday, "10:00:00", self.customer)
        response = self.client.get(
            f"/api/specialists/{self.specialist_a.id}/available-slots/",
            {"date": self.monday},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("09:00", response.data["slots"])

    def test_cancellation_releases_rescheduled_slot(self):
        appt = self._appt(t="10:00:00")
        self._reschedule(appt.id, self.next_monday, "11:00:00", self.customer)
        self.client.force_authenticate(user=self.customer)
        self.client.patch(f"/api/appointments/{appt.id}/cancel/")
        response = self.client.get(
            f"/api/specialists/{self.specialist_a.id}/available-slots/",
            {"date": self.next_monday},
        )
        self.assertIn("11:00", response.data["slots"])

    def test_existing_lifecycle_transitions_unaffected(self):
        """Confirm/cancel/complete still work after reschedule feature is added."""
        appt = self._appt(appt_status=AppointmentStatus.PENDING)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/appointments/{appt.id}/confirm/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        appt.refresh_from_db()
        self.assertEqual(appt.status, AppointmentStatus.CONFIRMED)

    def test_specialist_cannot_create_appointment_regression(self):
        """Specialist creation restriction must remain intact."""
        self.client.force_authenticate(user=self.spec_user_a)
        response = self.client.post(
            "/api/appointments/",
            {"specialist": self.specialist_a.id, "date": self.monday, "time": "10:00:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
