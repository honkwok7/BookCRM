from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import UserRole
from specialists.models import Specialist, WorkingHour, Weekday
from appointments.models import Appointment, AppointmentStatus


User = get_user_model()


def next_weekday(weekday: int) -> date:
    """Return the nearest future date that falls on the given weekday (0=Monday)."""
    today = date.today()
    days_ahead = weekday - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


class SpecialistAPITests(APITestCase):
	def setUp(self):
		self.specialist = Specialist.objects.create(
			name="Dr. Smith",
			profession="Cardiologist",
			description="Heart specialist",
		)
		self.admin = User.objects.create_superuser(email="admin@example.com", password="adminpass123", role=UserRole.ADMIN)

	def test_list_specialists(self):
		response = self.client.get("/api/specialists/")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(len(response.data.get("results", [])), 1)

	def test_search_specialists_by_profession(self):
		response = self.client.get("/api/specialists/?search=Cardio")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(len(response.data.get("results", [])), 1)

	def test_search_specialists_by_name(self):
		response = self.client.get("/api/specialists/?search=Smith")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(len(response.data["results"]), 1)

	def test_non_admin_cannot_create_specialist(self):
		user = User.objects.create_user(email="normal@example.com", password="strongpass123")
		self.client.force_authenticate(user=user)
		response = self.client.post(
			"/api/specialists/",
			{"name": "Dr. X", "profession": "Dentist", "description": "desc"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_admin_can_create_specialist(self):
		self.client.force_authenticate(user=self.admin)
		response = self.client.post(
			"/api/specialists/",
			{"name": "Dr. Jane", "profession": "Dentist", "description": "desc"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)

	# ------------------------------------------------------------------ #
	# slot_duration field                                                  #
	# ------------------------------------------------------------------ #

	def test_specialist_default_slot_duration(self):
		self.assertEqual(self.specialist.slot_duration, 30)

	def test_specialist_slot_duration_exposed_in_api(self):
		response = self.client.get(f"/api/specialists/{self.specialist.id}/")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertIn("slot_duration", response.data)
		self.assertEqual(response.data["slot_duration"], 30)

	def test_admin_can_set_custom_slot_duration(self):
		self.client.force_authenticate(user=self.admin)
		response = self.client.patch(
			f"/api/specialists/{self.specialist.id}/",
			{"slot_duration": 60},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.specialist.refresh_from_db()
		self.assertEqual(self.specialist.slot_duration, 60)

	def test_slot_duration_zero_is_rejected(self):
		self.client.force_authenticate(user=self.admin)
		response = self.client.patch(
			f"/api/specialists/{self.specialist.id}/",
			{"slot_duration": 0},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_slot_duration_negative_is_rejected(self):
		self.client.force_authenticate(user=self.admin)
		response = self.client.patch(
			f"/api/specialists/{self.specialist.id}/",
			{"slot_duration": -15},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class WorkingHourWriteAPITests(APITestCase):
	def setUp(self):
		self.specialist = Specialist.objects.create(name="Dr. House", profession="General")
		self.admin = User.objects.create_superuser(email="admin@example.com", password="adminpass123", role=UserRole.ADMIN)
		self.user = User.objects.create_user("patient", password="strongpass123")

	# ------------------------------------------------------------------ #
	# POST permissions                                                     #
	# ------------------------------------------------------------------ #

	def test_working_hour_post_requires_admin(self):
		self.client.force_authenticate(user=self.user)
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.MONDAY, "start_time": "09:00", "end_time": "12:00"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_working_hour_post_unauthenticated_is_forbidden(self):
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.MONDAY, "start_time": "09:00", "end_time": "12:00"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

	def test_admin_can_create_working_hour(self):
		self.client.force_authenticate(user=self.admin)
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.MONDAY, "start_time": "09:00", "end_time": "12:00"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		self.assertEqual(WorkingHour.objects.count(), 1)

	def test_create_returns_full_representation(self):
		self.client.force_authenticate(user=self.admin)
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.MONDAY, "start_time": "09:00", "end_time": "12:00"},
			format="json",
		)
		self.assertIn("day_display", response.data)
		self.assertIn("specialist", response.data)
		self.assertEqual(response.data["specialist"], self.specialist.id)

	def test_create_working_hour_invalid_start_after_end(self):
		self.client.force_authenticate(user=self.admin)
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.MONDAY, "start_time": "12:00", "end_time": "09:00"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_create_working_hour_invalid_same_start_end(self):
		self.client.force_authenticate(user=self.admin)
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.MONDAY, "start_time": "09:00", "end_time": "09:00"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	# ------------------------------------------------------------------ #
	# DELETE permissions                                                   #
	# ------------------------------------------------------------------ #

	def test_working_hour_delete_requires_admin(self):
		wh = WorkingHour.objects.create(
			specialist=self.specialist, day=Weekday.MONDAY,
			start_time=time(9, 0), end_time=time(12, 0),
		)
		self.client.force_authenticate(user=self.user)
		response = self.client.delete(
			f"/api/specialists/{self.specialist.id}/working-hours/{wh.id}/"
		)
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_admin_can_delete_working_hour(self):
		wh = WorkingHour.objects.create(
			specialist=self.specialist, day=Weekday.MONDAY,
			start_time=time(9, 0), end_time=time(12, 0),
		)
		self.client.force_authenticate(user=self.admin)
		response = self.client.delete(
			f"/api/specialists/{self.specialist.id}/working-hours/{wh.id}/"
		)
		self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
		self.assertFalse(WorkingHour.objects.filter(pk=wh.id).exists())

	def test_delete_wrong_specialist_returns_404(self):
		other = Specialist.objects.create(name="Dr. Other", profession="Dentist")
		wh = WorkingHour.objects.create(
			specialist=other, day=Weekday.MONDAY,
			start_time=time(9, 0), end_time=time(12, 0),
		)
		self.client.force_authenticate(user=self.admin)
		response = self.client.delete(
			f"/api/specialists/{self.specialist.id}/working-hours/{wh.id}/"
		)
		self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

	# ------------------------------------------------------------------ #
	# Overlap validation                                                   #
	# ------------------------------------------------------------------ #

	def test_overlapping_working_hours_are_rejected(self):
		WorkingHour.objects.create(
			specialist=self.specialist, day=Weekday.MONDAY,
			start_time=time(9, 0), end_time=time(12, 0),
		)
		self.client.force_authenticate(user=self.admin)
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.MONDAY, "start_time": "11:00", "end_time": "14:00"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_overlap_contained_range_is_rejected(self):
		WorkingHour.objects.create(
			specialist=self.specialist, day=Weekday.MONDAY,
			start_time=time(9, 0), end_time=time(17, 0),
		)
		self.client.force_authenticate(user=self.admin)
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.MONDAY, "start_time": "10:00", "end_time": "12:00"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_adjacent_working_hours_are_allowed(self):
		WorkingHour.objects.create(
			specialist=self.specialist, day=Weekday.MONDAY,
			start_time=time(9, 0), end_time=time(12, 0),
		)
		self.client.force_authenticate(user=self.admin)
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.MONDAY, "start_time": "12:00", "end_time": "15:00"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)

	def test_overlap_on_different_days_is_allowed(self):
		WorkingHour.objects.create(
			specialist=self.specialist, day=Weekday.MONDAY,
			start_time=time(9, 0), end_time=time(12, 0),
		)
		self.client.force_authenticate(user=self.admin)
		# Same range but Tuesday — should be fine
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.TUESDAY, "start_time": "09:00", "end_time": "12:00"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)

	def test_overlap_on_different_specialist_is_allowed(self):
		other = Specialist.objects.create(name="Dr. Other", profession="Dentist")
		WorkingHour.objects.create(
			specialist=other, day=Weekday.MONDAY,
			start_time=time(9, 0), end_time=time(12, 0),
		)
		self.client.force_authenticate(user=self.admin)
		response = self.client.post(
			f"/api/specialists/{self.specialist.id}/working-hours/",
			{"day": Weekday.MONDAY, "start_time": "09:00", "end_time": "12:00"},
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class AvailableSlotsAPITests(APITestCase):
	def setUp(self):
		self.specialist = Specialist.objects.create(
			name="Dr. House", profession="General", slot_duration=30
		)
		self.user = User.objects.create_user("patient", password="strongpass123")
		# Monday working hours: 09:00 – 12:00
		self.monday_wh = WorkingHour.objects.create(
			specialist=self.specialist,
			day=Weekday.MONDAY,
			start_time=time(9, 0),
			end_time=time(12, 0),
		)
		self.monday = next_weekday(Weekday.MONDAY)

	def _url(self, pk=None):
		pk = pk or self.specialist.id
		return f"/api/specialists/{pk}/available-slots/"

	# ------------------------------------------------------------------ #
	# Basic generation                                                     #
	# ------------------------------------------------------------------ #

	def test_slots_generated_correctly_30min(self):
		response = self.client.get(self._url(), {"date": self.monday})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		# 09:00–12:00 with 30-min slots → 6 slots; 12:00 must NOT appear
		self.assertEqual(response.data["slots"], [
			"09:00", "09:30", "10:00", "10:30", "11:00", "11:30"
		])

	def test_slots_generated_correctly_60min(self):
		self.specialist.slot_duration = 60
		self.specialist.save()
		response = self.client.get(self._url(), {"date": self.monday})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data["slots"], ["09:00", "10:00", "11:00"])

	def test_response_contains_metadata(self):
		response = self.client.get(self._url(), {"date": self.monday})
		self.assertEqual(response.data["specialist"], self.specialist.id)
		self.assertEqual(response.data["date"], str(self.monday))
		self.assertEqual(response.data["slot_duration"], 30)

	def test_last_slot_must_fit_inside_working_hour(self):
		"""12:00 must not appear because 12:00+30min > 12:00 (end)."""
		response = self.client.get(self._url(), {"date": self.monday})
		self.assertNotIn("12:00", response.data["slots"])

	def test_slots_are_chronological(self):
		response = self.client.get(self._url(), {"date": self.monday})
		slots = response.data["slots"]
		self.assertEqual(slots, sorted(slots))

	def test_no_working_hours_for_day_returns_empty_slots(self):
		tuesday = next_weekday(Weekday.TUESDAY)
		response = self.client.get(self._url(), {"date": tuesday})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data["slots"], [])

	def test_endpoint_is_public(self):
		# No authentication required
		response = self.client.get(self._url(), {"date": self.monday})
		self.assertEqual(response.status_code, status.HTTP_200_OK)

	# ------------------------------------------------------------------ #
	# Booked appointments affect availability                              #
	# ------------------------------------------------------------------ #

	def test_booked_pending_appointment_removes_slot(self):
		Appointment.objects.create(
			user=self.user, specialist=self.specialist,
			date=self.monday, time=time(10, 0),
			status=AppointmentStatus.PENDING,
		)
		response = self.client.get(self._url(), {"date": self.monday})
		self.assertNotIn("10:00", response.data["slots"])

	def test_booked_confirmed_appointment_removes_slot(self):
		Appointment.objects.create(
			user=self.user, specialist=self.specialist,
			date=self.monday, time=time(10, 0),
			status=AppointmentStatus.CONFIRMED,
		)
		response = self.client.get(self._url(), {"date": self.monday})
		self.assertNotIn("10:00", response.data["slots"])

	def test_cancelled_appointment_does_not_remove_slot(self):
		Appointment.objects.create(
			user=self.user, specialist=self.specialist,
			date=self.monday, time=time(10, 0),
			status=AppointmentStatus.CANCELLED,
		)
		response = self.client.get(self._url(), {"date": self.monday})
		self.assertIn("10:00", response.data["slots"])

	def test_multiple_booked_slots_removed(self):
		for t in [time(9, 0), time(10, 0), time(11, 0)]:
			Appointment.objects.create(
				user=self.user, specialist=self.specialist,
				date=self.monday, time=t,
				status=AppointmentStatus.PENDING,
			)
		response = self.client.get(self._url(), {"date": self.monday})
		self.assertEqual(response.data["slots"], ["09:30", "10:30", "11:30"])

	# ------------------------------------------------------------------ #
	# Multiple working hour ranges                                         #
	# ------------------------------------------------------------------ #

	def test_multiple_working_hour_ranges_merged(self):
		# Add afternoon block 14:00–15:00
		WorkingHour.objects.create(
			specialist=self.specialist, day=Weekday.MONDAY,
			start_time=time(14, 0), end_time=time(15, 0),
		)
		response = self.client.get(self._url(), {"date": self.monday})
		slots = response.data["slots"]
		self.assertIn("09:00", slots)
		self.assertIn("14:00", slots)
		# Gap in between must not have slots
		self.assertNotIn("12:30", slots)
		self.assertNotIn("13:00", slots)

	def test_multiple_ranges_result_is_chronological(self):
		WorkingHour.objects.create(
			specialist=self.specialist, day=Weekday.MONDAY,
			start_time=time(14, 0), end_time=time(15, 0),
		)
		response = self.client.get(self._url(), {"date": self.monday})
		slots = response.data["slots"]
		self.assertEqual(slots, sorted(slots))

	def test_no_duplicate_slots_from_overlapping_working_hours(self):
		"""If working hours overlap, the same time slot must appear only once."""
		WorkingHour.objects.create(
			specialist=self.specialist, day=Weekday.MONDAY,
			start_time=time(11, 0), end_time=time(13, 0),
		)
		response = self.client.get(self._url(), {"date": self.monday})
		slots = response.data["slots"]
		self.assertEqual(len(slots), len(set(slots)))
		# 11:00 and 11:30 each appear exactly once
		self.assertEqual(slots.count("11:00"), 1)
		self.assertEqual(slots.count("11:30"), 1)

	# ------------------------------------------------------------------ #
	# Input validation                                                     #
	# ------------------------------------------------------------------ #

	def test_missing_date_returns_400(self):
		response = self.client.get(self._url())
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_invalid_date_format_returns_400(self):
		response = self.client.get(self._url(), {"date": "28-08-2026"})
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_nonsense_date_returns_400(self):
		response = self.client.get(self._url(), {"date": "not-a-date"})
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_past_date_returns_400(self):
		response = self.client.get(self._url(), {"date": "2020-01-01"})
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_unknown_specialist_returns_404(self):
		response = self.client.get(self._url(pk=99999), {"date": self.monday})
		self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
