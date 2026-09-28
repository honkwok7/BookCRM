from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from appointments.models import Appointment, AppointmentStatus
from specialists.models import Specialist, WorkingHour


class SchedulingCorrectnessTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("scheduling-customer")
        cls.specialist = Specialist.objects.create(
            name="Scheduling specialist",
            profession="GP",
            slot_duration=30,
        )
        cls.day = timezone.localdate() + timedelta(days=7)
        cls.working = WorkingHour.objects.create(
            specialist=cls.specialist,
            day=cls.day.weekday(),
            start_time=time(9),
            end_time=time(17),
        )

    def setUp(self):
        self.client.force_authenticate(self.user)

    def existing(self, start="10:00", duration=60, **kwargs):
        return Appointment.objects.create(
            user=self.user,
            specialist=self.specialist,
            date=kwargs.pop("date", self.day),
            time=time.fromisoformat(start),
            duration=duration,
            **kwargs,
        )

    def create(self, start, duration=None, day=None):
        payload = {
            "specialist": self.specialist.pk,
            "date": str(day or self.day),
            "time": start,
        }
        if duration is not None:
            payload["duration"] = duration
        return self.client.post("/api/appointments/", payload, format="json")

    def reschedule(self, appointment, start, day=None):
        return self.client.patch(
            f"/api/appointments/{appointment.pk}/reschedule/",
            {"date": str(day or self.day), "time": start},
            format="json",
        )

    def slots(self, day=None):
        response = self.client.get(
            f"/api/specialists/{self.specialist.pk}/available-slots/",
            {"date": str(day or self.day)},
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["slots"]

    def assert_rejected(self, start, duration=30):
        """Check both mutation flows and default-duration availability."""
        count = Appointment.objects.count()
        response = self.create(start, duration)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(Appointment.objects.count(), count)
        moving = self.existing("09:00", duration, date=self.day + timedelta(days=7))
        original = (moving.date, moving.time, moving.duration)
        response = self.reschedule(moving, start)
        self.assertEqual(response.status_code, 400, response.data)
        moving.refresh_from_db()
        self.assertEqual((moving.date, moving.time, moving.duration), original)
        moving.delete()
        if duration == self.specialist.slot_duration and len(start) == 5:
            self.assertNotIn(start, self.slots())

    def split_hours(self):
        self.working.end_time = time(12)
        self.working.save()
        WorkingHour.objects.create(
            specialist=self.specialist,
            day=self.day.weekday(),
            start_time=time(14),
            end_time=time(18),
        )

    def test_same_start_rejected(self):
        self.existing()
        self.assert_rejected("10:00")

    def test_partial_overlap_at_beginning_rejected(self):
        self.existing("10:00", 60)
        self.assert_rejected("09:30", 60)

    def test_partial_overlap_at_end_rejected(self):
        self.existing("10:00", 60)
        self.assert_rejected("10:30", 60)

    def test_new_interval_containing_existing_rejected(self):
        self.existing("10:30", 30)
        self.assert_rejected("10:00", 120)

    def test_existing_interval_containing_new_rejected(self):
        self.existing("10:00", 120)
        self.assert_rejected("10:30", 30)

    def test_back_to_back_before_and_after_allowed(self):
        self.existing("10:00", 60)
        for start in ("09:30", "11:00"):
            with self.subTest(start=start):
                self.assertIn(start, self.slots())
                response = self.create(start, 30)
                self.assertEqual(response.status_code, 201, response.data)

    def test_sixty_minute_appointment_blocks_both_candidate_starts(self):
        for state in (AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED):
            with self.subTest(status=state):
                appointment = self.existing(status=state)
                slots = self.slots()
                self.assertNotIn("10:00", slots)
                self.assertNotIn("10:30", slots)
                self.assertIn("11:00", slots)
                appointment.delete()

    def test_terminal_statuses_release_complete_intervals(self):
        for state in (
            AppointmentStatus.CANCELLED,
            AppointmentStatus.COMPLETED,
            AppointmentStatus.NO_SHOW,
        ):
            with self.subTest(status=state):
                terminal = self.existing(status=state)
                self.assertIn("10:00", self.slots())
                self.assertIn("10:30", self.slots())
                response = self.create("10:30")
                self.assertEqual(response.status_code, 201, response.data)
                created = Appointment.objects.get(pk=response.data["id"])
                response = self.reschedule(created, "10:00")
                self.assertEqual(response.status_code, 200, response.data)
                created.delete()
                terminal.delete()

    def test_candidate_end_overlapping_later_start_is_unavailable(self):
        self.existing("10:45", 30)
        slots = self.slots()
        self.assertNotIn("10:30", slots)
        self.assertNotIn("11:00", slots)
        self.assertIn("11:30", slots)

    def test_thirty_minute_interval_at_last_slot_allowed(self):
        response = self.create("16:30", 30)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["duration"], 30)

    def test_sixty_minute_interval_ending_at_close_allowed(self):
        response = self.create("16:00", 60)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["duration"], 60)
        self.assertNotIn("16:30", self.slots())

    def test_duration_overflow_rejected(self):
        self.assert_rejected("16:30", 60)

    def test_exact_opening_boundary_allowed(self):
        self.assertIn("09:00", self.slots())
        response = self.create("09:00", 60)
        self.assertEqual(response.status_code, 201, response.data)

    def test_start_at_closing_boundary_rejected(self):
        self.assert_rejected("17:00")

    def test_partial_final_grid_slot_not_advertised_or_bookable(self):
        self.working.end_time = time(16, 45)
        self.working.save()
        self.assertIn("16:00", self.slots())
        self.assert_rejected("16:30")

    def test_break_crossing_rejected(self):
        self.split_hours()
        self.assert_rejected("11:30", 180)

    def test_multiple_intervals_allow_each_last_valid_slot(self):
        self.split_hours()
        for start in ("11:00", "17:00"):
            with self.subTest(start=start):
                response = self.create(start, 60)
                self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("12:00", self.slots())
        self.assertNotIn("13:30", self.slots())
        self.assertIn("14:00", self.slots())

    def test_nonzero_seconds_and_microseconds_rejected(self):
        for start in ("10:00:01", "10:30:01", "10:00:00.000001"):
            with self.subTest(start=start):
                self.assert_rejected(start)

    def test_unaligned_minutes_rejected(self):
        self.assert_rejected("10:15")

    def test_grid_uses_configured_duration_and_working_start(self):
        self.specialist.slot_duration = 45
        self.specialist.save()
        self.working.start_time = time(9, 15)
        self.working.save()
        self.assertIn("10:00", self.slots())
        self.assertIn("10:45", self.slots())
        self.assert_rejected("10:30", 45)
        response = self.create("10:00")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["duration"], 45)

    def test_grid_alignment_must_belong_to_the_fitting_interval(self):
        # Legacy overlapping hours: the short interval's grid must not validate
        # an appointment that only fits inside the other interval.
        self.working.end_time = time(10, 45)
        self.working.save()
        WorkingHour.objects.create(
            specialist=self.specialist,
            day=self.day.weekday(),
            start_time=time(10, 15),
            end_time=time(12),
        )
        self.assert_rejected("10:30", 60)

    def test_reschedule_excludes_own_overlapping_old_interval(self):
        appointment = self.existing("10:00", 60, notes="Preserve me")
        preserved = (
            appointment.pk,
            appointment.user_id,
            appointment.specialist_id,
            appointment.status,
            appointment.notes,
            appointment.duration,
            appointment.created_at,
        )
        response = self.reschedule(appointment, "10:30")
        self.assertEqual(response.status_code, 200, response.data)
        appointment.refresh_from_db()
        self.assertEqual(appointment.time, time(10, 30))
        self.assertEqual(
            (
                appointment.pk,
                appointment.user_id,
                appointment.specialist_id,
                appointment.status,
                appointment.notes,
                appointment.duration,
                appointment.created_at,
            ),
            preserved,
        )

    def test_reschedule_moves_complete_occupied_interval(self):
        appointment = self.existing("10:00", 60)
        response = self.reschedule(appointment, "14:00")
        self.assertEqual(response.status_code, 200, response.data)
        slots = self.slots()
        self.assertIn("10:00", slots)
        self.assertIn("10:30", slots)
        self.assertNotIn("14:00", slots)
        self.assertNotIn("14:30", slots)
        self.assertIn("15:00", slots)

    def test_reschedule_keeps_historical_duration_after_grid_change(self):
        appointment = self.existing("10:00", 60)
        self.specialist.slot_duration = 15
        self.specialist.save()
        response = self.reschedule(appointment, "16:15")
        self.assertEqual(response.status_code, 400, response.data)
        response = self.reschedule(appointment, "16:00")
        self.assertEqual(response.status_code, 200, response.data)
        appointment.refresh_from_db()
        self.assertEqual(appointment.duration, 60)
        self.assertNotIn("16:45", self.slots())

    def test_cross_midnight_legacy_interval_blocks_next_day(self):
        self.existing("23:30", 660, date=self.day - timedelta(days=1))
        self.assert_rejected("10:00")
        self.assertIn("10:30", self.slots())

    def test_new_interval_cannot_cross_midnight(self):
        self.assert_rejected("16:00", 600)

    def test_other_specialist_and_nonoverlapping_dates_do_not_block(self):
        other = Specialist.objects.create(name="Other", profession="GP")
        Appointment.objects.create(
            user=self.user,
            specialist=other,
            date=self.day,
            time=time(10),
            duration=60,
        )
        self.existing(date=self.day - timedelta(days=1))
        self.existing(date=self.day + timedelta(days=1))
        self.assertIn("10:00", self.slots())
        response = self.create("10:00", 60)
        self.assertEqual(response.status_code, 201, response.data)

    def test_nonpositive_duration_rejected(self):
        for duration in (0, -1):
            with self.subTest(duration=duration):
                response = self.create("10:00", duration)
                self.assertEqual(response.status_code, 400, response.data)

    def test_oversized_duration_returns_validation_error(self):
        response = self.create("10:00", 2147483647)
        self.assertEqual(response.status_code, 400, response.data)

    def test_last_supported_date_does_not_overflow_slot_generation(self):
        self.working.day = date.max.weekday()
        self.working.start_time = time(23)
        self.working.end_time = time(23, 59)
        self.working.save()
        self.assertEqual(self.slots(day=date.max), ["23:00"])
        response = self.create("23:30", 60, day=date.max)
        self.assertEqual(response.status_code, 400, response.data)

    def test_past_date_restrictions_preserved(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        response = self.create("10:00", day=yesterday)
        self.assertEqual(response.status_code, 400, response.data)
        appointment = self.existing()
        response = self.reschedule(appointment, "11:00", day=yesterday)
        self.assertEqual(response.status_code, 400, response.data)
        response = self.client.get(
            f"/api/specialists/{self.specialist.pk}/available-slots/",
            {"date": str(yesterday)},
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_today_policy_preserved_in_non_utc_timezone(self):
        # 06:30 UTC is 10:00 Tehran local time. Availability historically lists
        # today's grid; mutations reject starts at or before the current time.
        now = datetime.combine(self.day, time(6, 30), tzinfo=UTC)
        with timezone.override("Asia/Tehran"), patch("django.utils.timezone.now", return_value=now):
            appointment = self.existing("14:00")
            self.assertIn("09:00", self.slots())
            for start in ("09:30", "10:00"):
                with self.subTest(start=start):
                    response = self.create(start)
                    self.assertEqual(response.status_code, 400, response.data)
                    response = self.reschedule(appointment, start)
                    self.assertEqual(response.status_code, 400, response.data)
            response = self.create("10:30")
            self.assertEqual(response.status_code, 201, response.data)
            response = self.reschedule(appointment, "11:00")
            self.assertEqual(response.status_code, 200, response.data)
