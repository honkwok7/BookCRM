from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from threading import Event, local
from time import monotonic
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, connection, connections
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase

from accounts.models import UserRole
from appointments import transactions as mutations
from appointments.models import Appointment, AppointmentStatus
from appointments.serializers import AppointmentSerializer
from specialists.models import Specialist, WorkingHour


class MutationFixtures:
    def make_fixtures(self):
        self.user = get_user_model().objects.create_user(
            "mutation-customer", role=UserRole.CUSTOMER
        )
        self.admin = get_user_model().objects.create_user("mutation-admin", role=UserRole.ADMIN)
        self.specialist = Specialist.objects.create(name="Mutation specialist", profession="GP")
        self.day = timezone.localdate() + timedelta(days=7)
        WorkingHour.objects.create(
            specialist=self.specialist,
            day=self.day.weekday(),
            start_time=time(9),
            end_time=time(17),
        )
        self.client.force_authenticate(self.user)

    def appointment(self, start="09:00", duration=60, state=AppointmentStatus.PENDING):
        return Appointment.objects.create(
            user=self.user,
            specialist=self.specialist,
            date=self.day,
            time=time.fromisoformat(start),
            duration=duration,
            status=state,
            notes="Preserved notes",
        )

    def creation(self, start="10:00", duration=60):
        return (
            "post",
            "/api/appointments/",
            {
                "specialist": self.specialist.pk,
                "date": str(self.day),
                "time": start,
                "duration": duration,
            },
        )

    def action(self, appointment, action, start="10:00"):
        return (
            "patch",
            f"/api/appointments/{appointment.pk}/{action}/",
            (
                {
                    "date": str(self.day),
                    "time": start,
                }
                if action == "reschedule"
                else {}
            ),
        )

    def request(self, operation, client=None):
        method, url, data = operation
        if client is None:
            self.client.force_authenticate(self.user if method == "post" else self.admin)
        return getattr(client or self.client, method)(url, data, format="json")


class MutationTransactionTests(MutationFixtures, APITestCase):
    """Portable transaction regressions; these do not claim real row locking."""

    def setUp(self):
        self.make_fixtures()

    def test_creation_rechecks_overlap_after_lock(self):
        original = mutations.lock_specialist

        def lock(pk):
            self.appointment("10:00")
            return original(pk)

        with patch.object(mutations, "lock_specialist", side_effect=lock):
            response = self.request(self.creation("10:30"))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("overlaps", str(response.data))
        # The synthetic competing write is also rolled back by this transaction.
        self.assertEqual(Appointment.objects.count(), 0)

    def test_creation_uses_fresh_specialist_grid(self):
        original = mutations.lock_specialist

        def lock(pk):
            Specialist.objects.filter(pk=pk).update(slot_duration=60)
            return original(pk)

        with patch.object(mutations, "lock_specialist", side_effect=lock):
            response = self.request(self.creation("10:30"))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("60-minute", str(response.data))
        self.assertFalse(Appointment.objects.exists())

    def test_creation_revalidation_and_save_share_transaction(self):
        depth = len(connection.atomic_blocks)
        phases = []
        validate = AppointmentSerializer.validate
        save = Appointment.save

        def checked_validate(serializer, attrs):
            phases.append(("validate", len(connection.atomic_blocks)))
            return validate(serializer, attrs)

        def checked_save(appointment, *args, **kwargs):
            phases.append(("save", len(connection.atomic_blocks)))
            return save(appointment, *args, **kwargs)

        with (
            patch.object(AppointmentSerializer, "validate", checked_validate),
            patch.object(Appointment, "save", checked_save),
        ):
            response = self.request(self.creation())
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            phases, [("validate", depth + 1), ("validate", depth + 1), ("save", depth + 1)]
        )

    def test_reschedule_rereads_status_after_schedule_lock(self):
        appointment = self.appointment()
        original = mutations.lock_specialist

        def lock(pk):
            Appointment.objects.filter(pk=appointment.pk).update(status=AppointmentStatus.CANCELLED)
            return original(pk)

        with patch.object(mutations, "lock_specialist", side_effect=lock):
            response = self.request(self.action(appointment, "reschedule"))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("cancelled", str(response.data))
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CANCELLED)
        self.assertEqual(appointment.time, time(9))

    def test_reschedule_rereads_duration_after_schedule_lock(self):
        appointment = self.appointment(duration=30)
        original = mutations.lock_specialist

        def lock(pk):
            Appointment.objects.filter(pk=appointment.pk).update(duration=120)
            return original(pk)

        with patch.object(mutations, "lock_specialist", side_effect=lock):
            response = self.request(self.action(appointment, "reschedule", "16:00"))
        self.assertEqual(response.status_code, 400, response.data)
        appointment.refresh_from_db()
        self.assertEqual(appointment.time, time(9))

    def test_lifecycle_uses_locked_current_status(self):
        for action in ("confirm", "cancel", "complete", "no-show"):
            with self.subTest(action=action):
                appointment = self.appointment(state=AppointmentStatus.CONFIRMED)
                original = mutations.lock_appointment

                def lock(pk, original=original, **kwargs):
                    Appointment.objects.filter(pk=pk).update(status=AppointmentStatus.COMPLETED)
                    return original(pk, **kwargs)

                with patch.object(mutations, "lock_appointment", side_effect=lock):
                    response = self.request(self.action(appointment, action))
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("completed", str(response.data))
                appointment.refresh_from_db()
                self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)
                appointment.delete()

    def test_locks_are_inside_mutation_transaction_and_ordered(self):
        appointment = self.appointment()
        depth = len(connection.atomic_blocks)
        events = []
        from django.db.models.query import QuerySet

        original = QuerySet.get

        def get(queryset, *args, **kwargs):
            if queryset.query.select_for_update:
                events.append(queryset.model)
                self.assertGreater(len(connection.atomic_blocks), depth)
            return original(queryset, *args, **kwargs)

        with patch.object(QuerySet, "get", get):
            response = self.request(self.action(appointment, "reschedule"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(events, [Specialist, Appointment])
        events.clear()
        with patch.object(QuerySet, "get", get):
            response = self.request(self.action(appointment, "confirm"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(events, [Appointment])

    def test_mutations_save_only_intended_fields(self):
        save = Appointment.save

        def save_with_intervening_notes(appointment, *args, **kwargs):
            Appointment.objects.filter(pk=appointment.pk).update(notes="Newer notes")
            return save(appointment, *args, **kwargs)

        for action in ("confirm", "cancel", "complete", "no-show", "reschedule"):
            with self.subTest(action=action):
                state = (
                    AppointmentStatus.PENDING
                    if action == "confirm"
                    else AppointmentStatus.CONFIRMED
                )
                appointment = self.appointment(state=state)
                with patch.object(Appointment, "save", save_with_intervening_notes):
                    response = self.request(self.action(appointment, action))
                self.assertEqual(response.status_code, 200, response.data)
                appointment.refresh_from_db()
                self.assertEqual(appointment.notes, "Newer notes")
                appointment.delete()

    def conflict(self, *, operational=False, sqlstate=None):
        if operational:
            cause = Exception("private database diagnostics")
            cause.sqlstate = sqlstate
            error = OperationalError("private database diagnostics")
        else:
            cause = Exception("private database diagnostics")
            cause.diag = SimpleNamespace(constraint_name="unique_active_appointment_slot")
            error = IntegrityError("private database diagnostics")
        error.__cause__ = cause
        return error

    def test_database_conflicts_rollback_every_mutation_and_return_400(self):
        save = Appointment.save
        for action in ("create", "confirm", "cancel", "complete", "no-show", "reschedule"):
            with self.subTest(action=action):
                appointment = None
                if action != "create":
                    state = (
                        AppointmentStatus.PENDING
                        if action == "confirm"
                        else AppointmentStatus.CONFIRMED
                    )
                    appointment = self.appointment(state=state)
                before = list(Appointment.objects.values())

                def failed_save(instance, *args, **kwargs):
                    save(instance, *args, **kwargs)
                    raise self.conflict()

                with patch.object(Appointment, "save", failed_save):
                    operation = (
                        self.creation() if action == "create" else self.action(appointment, action)
                    )
                    response = self.request(operation)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("non_field_errors", response.data)
                self.assertNotIn("private", str(response.data))
                self.assertEqual(list(Appointment.objects.values()), before)
                if appointment:
                    appointment.delete()

    def test_postgres_transaction_conflicts_have_controlled_responses(self):
        for sqlstate in ("40001", "40P01", "55P03"):
            with (
                self.subTest(sqlstate=sqlstate),
                patch.object(
                    mutations,
                    "lock_specialist",
                    side_effect=self.conflict(operational=True, sqlstate=sqlstate),
                ),
            ):
                response = self.request(self.creation())
                self.assertEqual(response.status_code, 400, response.data)
                self.assertNotIn("private", str(response.data))
        self.assertFalse(Appointment.objects.exists())

    def test_sqlite_lock_conflicts_have_controlled_responses(self):
        for code in (5, 6, 517):
            cause = Exception("private database diagnostics")
            cause.sqlite_errorcode = code
            error = OperationalError("private database diagnostics")
            error.__cause__ = cause
            with (
                self.subTest(code=code),
                patch.object(mutations, "lock_specialist", side_effect=error),
            ):
                response = self.request(self.creation())
                self.assertEqual(response.status_code, 400, response.data)
                self.assertNotIn("private", str(response.data))

    def test_initial_creation_validation_lock_error_is_translated(self):
        cause = Exception("private database diagnostics")
        cause.sqlite_errorcode = 6
        error = OperationalError("private database diagnostics")
        error.__cause__ = cause
        with patch.object(AppointmentSerializer, "validate", side_effect=error):
            response = self.request(self.creation())
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNotIn("private", str(response.data))
        self.assertFalse(Appointment.objects.exists())

    def test_sqlite_unique_constraint_conflict_is_translated(self):
        error = IntegrityError(
            "UNIQUE constraint failed: appointments_appointment.specialist_id, "
            "appointments_appointment.date, appointments_appointment.time"
        )
        with patch.object(Appointment, "save", side_effect=error):
            response = self.request(self.creation())
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("non_field_errors", response.data)

    def test_unrelated_database_errors_are_not_misclassified(self):
        for error in (IntegrityError("unrelated constraint"), OperationalError("connection lost")):
            with self.subTest(error=error), self.assertRaises(type(error)):
                with mutations.mutation_transaction():
                    raise error
        self.assertEqual(Appointment.objects.count(), 0)


@skipUnless(
    connection.vendor == "postgresql",
    "Requires PostgreSQL row locks and pg_blocking_pids; SQLite cannot prove these races.",
)
class PostgreSQLMutationRaceTests(MutationFixtures, APITransactionTestCase):
    """Separate connections, controlled ordering, and observed database waits."""

    def setUp(self):
        self.make_fixtures()

    def race(self, first_operation, second_operation, lock_name="lock_specialist"):
        first_locked, second_entered, release = Event(), Event(), Event()
        context = local()
        pids = {}
        original = getattr(mutations, lock_name)

        def gated_lock(*args, **kwargs):
            if context.label == "second":
                second_entered.set()
                return original(*args, **kwargs)
            result = original(*args, **kwargs)
            first_locked.set()
            if not release.wait(10):
                raise AssertionError("Timed out releasing first transaction")
            return result

        def worker(label, operation):
            context.label = label
            connections.close_all()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[label] = cursor.fetchone()[0]
                client = APIClient()
                actor = self.user if operation[0] == "post" else self.admin
                client.force_authenticate(get_user_model().objects.get(pk=actor.pk))
                response = self.request(operation, client)
                return response.status_code, response.data
            finally:
                connections.close_all()

        with (
            patch.object(mutations, lock_name, gated_lock),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(worker, "first", first_operation)
            second = None
            try:
                self.assertTrue(first_locked.wait(10), "First request never acquired its lock")
                second = pool.submit(worker, "second", second_operation)
                self.assertTrue(second_entered.wait(10), "Second request never attempted the lock")
                deadline = monotonic() + 10
                while True:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_blocking_pids(%s)", [pids["second"]])
                        blockers = cursor.fetchone()[0]
                    if pids["first"] in blockers:
                        break
                    self.assertFalse(
                        second.done(), "Competing request did not wait for the database lock"
                    )
                    self.assertLess(monotonic(), deadline, "No PostgreSQL lock wait observed")
            finally:
                release.set()
            return first.result(timeout=10), second.result(timeout=10)

    def assert_conflict(self, results, first_status):
        self.assertEqual(results[0][0], first_status, results)
        self.assertEqual(results[1][0], 400, results)
        self.assertNotIn("Traceback", str(results[1][1]))

    def test_same_slot_concurrent_creation(self):
        self.assert_conflict(self.race(self.creation(), self.creation()), 201)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_overlapping_concurrent_creation(self):
        self.assert_conflict(self.race(self.creation(), self.creation("10:30")), 201)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_two_reschedules_compete_for_destination(self):
        first, second = self.appointment("09:00"), self.appointment("14:00")
        self.assert_conflict(
            self.race(
                self.action(first, "reschedule", "11:00"),
                self.action(second, "reschedule", "11:30"),
            ),
            200,
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.time, time(11))
        self.assertEqual(second.time, time(14))

    def test_creation_wins_against_reschedule(self):
        appointment = self.appointment("14:00")
        self.assert_conflict(
            self.race(self.creation(), self.action(appointment, "reschedule", "10:30")), 201
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.time, time(14))

    def test_reschedule_wins_against_creation(self):
        appointment = self.appointment("14:00")
        self.assert_conflict(
            self.race(self.action(appointment, "reschedule"), self.creation("10:30")), 200
        )
        self.assertEqual(Appointment.objects.count(), 1)

    def test_lifecycle_competition_cannot_overwrite_terminal_state(self):
        appointment = self.appointment(state=AppointmentStatus.CONFIRMED)
        self.assert_conflict(
            self.race(
                self.action(appointment, "complete"),
                self.action(appointment, "no-show"),
                lock_name="lock_appointment",
            ),
            200,
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)

    def test_cancellation_cannot_be_overwritten_by_reschedule(self):
        appointment = self.appointment()
        self.assert_conflict(
            self.race(
                self.action(appointment, "cancel"),
                self.action(appointment, "reschedule"),
                lock_name="lock_appointment",
            ),
            200,
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CANCELLED)
        self.assertEqual(appointment.time, time(9))

    def test_lifecycle_preserves_concurrently_rescheduled_time(self):
        appointment = self.appointment(state=AppointmentStatus.CONFIRMED)
        results = self.race(
            self.action(appointment, "reschedule", "11:00"),
            self.action(appointment, "complete"),
            lock_name="lock_appointment",
        )
        self.assertEqual([result[0] for result in results], [200, 200], results)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)
        self.assertEqual(appointment.time, time(11))
