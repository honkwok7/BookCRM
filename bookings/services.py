from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from bookings.models import Booking, BookingActivityLog, BookingStatusHistory, Customer
from core.services import write_audit_log
from notifications.services import queue_booking_notification

ACTIVE_BOOKING_STATUSES = [
    Booking.Status.PENDING,
    Booking.Status.CONFIRMED,
    Booking.Status.CHECKED_IN,
    Booking.Status.IN_PROGRESS,
]


def _ensure_customer(*, organization, name, email, phone="", user=None):
    customer, _ = Customer.objects.get_or_create(
        organization=organization,
        email=email,
        defaults={"name": name, "phone": phone, "user": user},
    )
    return customer


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
):
    if start_datetime <= timezone.now():
        raise ValueError("Cannot book in the past")

    duration = timedelta(minutes=service.duration_minutes)
    end_datetime = start_datetime + duration

    conflict_exists = (
        Booking.objects.select_for_update()
        .filter(
            organization=organization,
            staff=staff_profile,
            status__in=ACTIVE_BOOKING_STATUSES,
            start_datetime__lt=end_datetime,
            end_datetime__gt=start_datetime,
        )
        .exists()
    )
    if conflict_exists:
        raise ValueError("Selected slot is no longer available")

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
    write_audit_log(
        action="booking.created",
        organization=organization,
        user=actor,
        object_type="Booking",
        object_identifier=str(booking.id),
        metadata={"reference": booking.reference},
    )
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
    if booking.status == Booking.Status.CANCELLED:
        return booking
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
    queue_booking_notification(
        booking=booking,
        notification_type="booking_cancellation",
        subject=f"Booking cancelled: {booking.reference}",
        template_base="booking_cancellation",
        recipient_email=booking.customer_email,
        recipient_user=booking.customer.user if booking.customer else None,
    )
    return booking
