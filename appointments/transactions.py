"""Transaction boundaries and locks shared by appointment API mutations."""

from contextlib import contextmanager

from django.db import IntegrityError, OperationalError, transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.settings import api_settings

from appointments.models import Appointment
from specialists.models import Specialist


def _is_concurrency_conflict(error):
    cause = error.__cause__
    if isinstance(error, IntegrityError):
        constraint = getattr(getattr(cause, "diag", None), "constraint_name", None)
        return constraint == "unique_active_appointment_slot" or str(error) == (
            "UNIQUE constraint failed: appointments_appointment.specialist_id, "
            "appointments_appointment.date, appointments_appointment.time"
        )
    sqlstate = getattr(cause, "sqlstate", None)
    sqlite_code = getattr(cause, "sqlite_errorcode", 0)
    return sqlstate in {"40001", "40P01", "55P03"} or sqlite_code & 0xFF in {5, 6}


@contextmanager
def mutation_transaction():
    try:
        with transaction.atomic():
            yield
    except (IntegrityError, OperationalError) as error:
        # Translate only recognized conflicts, after atomic has rolled back.
        # In particular, SQLite cannot upgrade every competing read transaction.
        if not _is_concurrency_conflict(error):
            raise
        raise ValidationError(
            {
                api_settings.NON_FIELD_ERRORS_KEY: [
                    "Appointment conflict. Refresh availability and retry."
                ],
            }
        ) from error


def lock_specialist(pk):
    # The parent row exists even when the destination has no appointments.
    # All API creates and reschedules acquire it before checking occupancy.
    return get_object_or_404(Specialist.objects.select_for_update(), pk=pk)


def lock_appointment(pk, *, schedule=False):
    specialist = None
    if schedule:
        # This read only locates the lock resource; no mutable state is trusted.
        reference = get_object_or_404(Appointment.objects.only("specialist_id"), pk=pk)
        specialist = lock_specialist(reference.specialist_id)
    # Order is always specialist -> appointment when both locks are required.
    appointment = get_object_or_404(Appointment.objects.select_for_update(), pk=pk)
    if specialist is not None:
        if appointment.specialist_id != specialist.pk:
            raise ValidationError(
                {
                    api_settings.NON_FIELD_ERRORS_KEY: ["Appointment changed. Refresh and retry."],
                }
            )
        appointment.specialist = specialist
    return appointment
