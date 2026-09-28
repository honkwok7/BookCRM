from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from bookings.models import Booking, BookingActivityLog, BookingStatusHistory, Customer
from core.audit import AuditAction, record_audit
from core.exceptions import ConflictError, DomainError
from notifications.services import queue_booking_notification
from staff.models import StaffProfile

ACTIVE_BOOKING_STATUSES = [
    Booking.Status.PENDING,
    Booking.Status.CONFIRMED,
    Booking.Status.CHECKED_IN,
    Booking.Status.IN_PROGRESS,
]

# Lifecycle rules (docs/BOOKING_ENGINE.md). Terminal statuses have no outgoing transitions.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    Booking.Status.PENDING: frozenset(
        {Booking.Status.CONFIRMED, Booking.Status.CANCELLED, Booking.Status.REJECTED}
    ),
    Booking.Status.CONFIRMED: frozenset(
        {Booking.Status.CHECKED_IN, Booking.Status.CANCELLED, Booking.Status.NO_SHOW}
    ),
    Booking.Status.CHECKED_IN: frozenset(
        {Booking.Status.IN_PROGRESS, Booking.Status.COMPLETED, Booking.Status.NO_SHOW}
    ),
    Booking.Status.IN_PROGRESS: frozenset({Booking.Status.COMPLETED}),
    Booking.Status.COMPLETED: frozenset(),
    Booking.Status.CANCELLED: frozenset(),
    Booking.Status.NO_SHOW: frozenset(),
    Booking.Status.REJECTED: frozenset(),
}
RESCHEDULABLE_STATUSES = frozenset({Booking.Status.PENDING, Booking.Status.CONFIRMED})


def _ensure_customer(*, organization, name, email, phone="", user=None):
    customer, _ = Customer.objects.get_or_create(
        organization=organization,
        email=email,
        defaults={"name": name, "phone": phone, "user": user},
    )
    return customer


def lock_staff(staff_id) -> StaffProfile:
    """Lock the staff member's calendar for the rest of the transaction.

    The staff row is the lock resource for every create and reschedule, because it exists even
    when the destination time has no bookings to lock. Concurrent writers to one calendar
    therefore queue here, and each re-checks overlaps against committed data.
    """
    return StaffProfile.objects.select_for_update().get(pk=staff_id)


def lock_booking(booking: Booking) -> Booking:
    """Re-read ``booking`` under a row lock so decisions use its committed state.

    Only the booking row is locked (``of=("self",)``): PostgreSQL refuses FOR UPDATE on the
    nullable side of the outer join that ``select_related("customer")`` produces.
    """
    return (
        Booking.objects.select_for_update(of=("self",))
        .select_related("organization", "customer")
        .get(pk=booking.pk)
    )


# Lock order, to rule out deadlocks: staff calendar first, then the booking row.


@transaction.atomic
def create_booking(
    *,
    organization,
    service,
    staff_profile,
    customer_name,
    customer_email,
    customer_phone,
    start_datetime,
    customer_timezone="UTC",
    customer_notes="",
    actor=None,
    customer_user=None,
    notify: bool = True,
):
    if start_datetime <= timezone.now():
        raise DomainError("Cannot book in the past", code="in_past")

    duration = timedelta(minutes=service.duration_minutes)
    end_datetime = start_datetime + duration

    # Serialize writers to this calendar, then check overlaps (half-open [start, end)).
    # A database-level exclusion constraint adds a second guarantee in M4.2.
    lock_staff(staff_profile.pk)
    conflict_exists = Booking.objects.filter(
        organization=organization,
        staff=staff_profile,
        status__in=ACTIVE_BOOKING_STATUSES,
        start_datetime__lt=end_datetime,
        end_datetime__gt=start_datetime,
    ).exists()
    if conflict_exists:
        raise ConflictError("Selected slot is no longer available", code="slot_unavailable")

    customer = _ensure_customer(
        organization=organization,
        name=customer_name,
        email=customer_email,
        phone=customer_phone,
        user=customer_user,
    )

    tzname = organization.timezone
    reference = Booking.generate_reference(year=start_datetime.astimezone(ZoneInfo(tzname)).year)
    while Booking.objects.filter(reference=reference).exists():
        reference = Booking.generate_reference(
            year=start_datetime.astimezone(ZoneInfo(tzname)).year
        )

    booking = Booking.objects.create(
        reference=reference,
        organization=organization,
        customer=customer,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        service=service,
        staff=staff_profile,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        customer_timezone=customer_timezone,
        organization_timezone=organization.timezone,
        price_snapshot=service.price,
        duration_snapshot_minutes=service.duration_minutes,
        customer_notes=customer_notes,
        status=Booking.Status.CONFIRMED,
    )

    BookingStatusHistory.objects.create(
        booking=booking, old_status="", new_status=booking.status, changed_by=actor
    )
    BookingActivityLog.objects.create(
        booking=booking,
        organization=organization,
        actor=actor,
        action="booking.created",
        metadata={"service": str(service.id), "staff": str(staff_profile.id)},
    )
    record_audit(
        AuditAction.BOOKING_CREATED,
        organization=organization,
        actor=actor,
        object_type="Booking",
        object_identifier=str(booking.id),
        metadata={"reference": booking.reference},
    )
    if notify:
        queue_booking_notification(
            booking=booking,
            notification_type="booking_confirmation",
            subject=f"Booking confirmed: {booking.reference}",
            template_base="booking_confirmation",
            recipient_email=booking.customer_email,
            recipient_user=customer_user,
        )
    return booking


@transaction.atomic
def cancel_booking(*, booking: Booking, actor=None, reason: str = ""):
    booking = lock_booking(booking)
    if booking.status == Booking.Status.CANCELLED:
        return booking
    if Booking.Status.CANCELLED not in ALLOWED_TRANSITIONS[booking.status]:
        raise ConflictError(
            f"A {booking.get_status_display().lower()} appointment cannot be cancelled",
            code="invalid_transition",
        )
    old_status = booking.status
    booking.status = Booking.Status.CANCELLED
    booking.cancellation_reason = reason
    booking.cancelled_by = actor
    booking.cancelled_at = timezone.now()
    booking.save(
        update_fields=[
            "status",
            "cancellation_reason",
            "cancelled_by",
            "cancelled_at",
            "updated_at",
        ]
    )
    BookingStatusHistory.objects.create(
        booking=booking,
        old_status=old_status,
        new_status=booking.status,
        changed_by=actor,
        note=reason,
    )
    BookingActivityLog.objects.create(
        booking=booking,
        organization=booking.organization,
        actor=actor,
        action="booking.cancelled",
        metadata={"reason": reason},
    )
    record_audit(
        AuditAction.BOOKING_CANCELLED,
        organization=booking.organization,
        actor=actor,
        object_type="Booking",
        object_identifier=str(booking.id),
        metadata={"reference": booking.reference},
    )
    queue_booking_notification(
        booking=booking,
        notification_type="booking_cancellation",
        subject=f"Booking cancelled: {booking.reference}",
        template_base="booking_cancellation",
        recipient_email=booking.customer_email,
        recipient_user=booking.customer.user if booking.customer else None,
    )
    return booking


@transaction.atomic
def change_booking_status(*, booking: Booking, new_status: str, actor=None, note: str = ""):
    if new_status not in Booking.Status.values:
        raise DomainError(f"Unknown status '{new_status}'", code="invalid_status")
    if new_status == Booking.Status.CANCELLED:
        return cancel_booking(booking=booking, actor=actor, reason=note)

    booking = lock_booking(booking)
    if new_status not in ALLOWED_TRANSITIONS[booking.status]:
        raise ConflictError(
            f"Cannot change status from {booking.status} to {new_status}",
            code="invalid_transition",
        )
    old_status = booking.status
    booking.status = new_status
    booking.save(update_fields=["status", "updated_at"])
    BookingStatusHistory.objects.create(
        booking=booking, old_status=old_status, new_status=new_status, changed_by=actor, note=note
    )
    record_audit(
        AuditAction.BOOKING_STATUS_CHANGED,
        organization=booking.organization,
        actor=actor,
        object_type="Booking",
        object_identifier=str(booking.id),
        metadata={"from": old_status, "to": new_status},
    )
    return booking


@transaction.atomic
def reschedule_booking(*, booking: Booking, new_start, actor=None) -> Booking:
    """Move an appointment to ``new_start``: one transaction, all-or-nothing.

    The old appointment is closed as cancelled ("Rescheduled") and a new one is created and
    linked via ``rescheduled_from``. If the new time is rejected, nothing changes.
    """
    lock_staff(booking.staff_id)
    booking = lock_booking(booking)
    if booking.status not in RESCHEDULABLE_STATUSES:
        raise ConflictError(
            f"A {booking.get_status_display().lower()} appointment cannot be rescheduled",
            code="invalid_transition",
        )

    old_status = booking.status
    booking.status = Booking.Status.CANCELLED
    booking.cancellation_reason = "Rescheduled"
    booking.cancelled_by = actor
    booking.cancelled_at = timezone.now()
    booking.save(
        update_fields=[
            "status",
            "cancellation_reason",
            "cancelled_by",
            "cancelled_at",
            "updated_at",
        ]
    )
    BookingStatusHistory.objects.create(
        booking=booking,
        old_status=old_status,
        new_status=booking.status,
        changed_by=actor,
        note="Rescheduled",
    )

    new_booking = create_booking(
        organization=booking.organization,
        service=booking.service,
        staff_profile=booking.staff,
        customer_name=booking.customer_name,
        customer_email=booking.customer_email,
        customer_phone=booking.customer_phone,
        start_datetime=new_start,
        customer_timezone=booking.customer_timezone,
        customer_notes=booking.customer_notes,
        actor=actor,
        customer_user=booking.customer.user if booking.customer else None,
    )
    new_booking.rescheduled_from = booking
    new_booking.save(update_fields=["rescheduled_from", "updated_at"])
    record_audit(
        AuditAction.BOOKING_RESCHEDULED,
        organization=booking.organization,
        actor=actor,
        object_type="Booking",
        object_identifier=str(new_booking.id),
        metadata={"from_booking": str(booking.id)},
    )
    return new_booking
