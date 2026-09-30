"""The booking service: the only code that creates appointments or changes their status or times.

Every entry point (API, public page, reception screens, ``seed_demo``, future AI agents) calls
these functions, so the same rules apply everywhere:

- tenant consistency (service, staff, location and customer belong to the organization);
- the staff member's calendar is locked, then the time is re-checked by the availability
  engine (hours, location, closures, time off, other appointments and buffers, daily limit;
  public callers also notice and booking window);
- the provider's own duration and price for the service are used and snapshotted;
- the plan's monthly booking limit;
- an optional idempotency key makes retries safe;
- history, audit, CRM timeline and notifications (queued ``on_commit``).

On PostgreSQL the ``booking_staff_no_overlap`` exclusion constraint backs the lock up: two
active appointments of one staff member can never overlap, whatever writes them.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from bookings.models import Booking, BookingActivityLog, BookingStatusHistory, Customer
from core.audit import AuditAction, record_audit
from core.exceptions import ConflictError, DomainError
from crm.activity import Kind, booking_metadata, record_activity
from crm.services import find_or_create_customer
from locations.models import Location
from notifications.services import queue_booking_notification
from scheduling.availability import AvailabilityService
from staff.models import StaffProfile
from staff.selectors import offering_for

OVERLAP_CONSTRAINT = "booking_staff_no_overlap"
IDEMPOTENCY_CONSTRAINT = "booking_unique_idempotency_key_per_org"

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
# Check-in, start and completion are allowed from this long before the start time.
CHECK_IN_EARLIEST = timedelta(hours=1)
ARRIVAL_STATUSES = frozenset(
    {Booking.Status.CHECKED_IN, Booking.Status.IN_PROGRESS, Booking.Status.COMPLETED}
)
# Final statuses a past appointment can be imported with (``record_past_booking``).
PAST_STATUSES = frozenset(
    {Booking.Status.COMPLETED, Booking.Status.NO_SHOW, Booking.Status.CANCELLED}
)
# The CRM source of a customer first seen through a booking from this source.
CUSTOMER_SOURCE = {
    Booking.Source.PUBLIC_BOOKING: Customer.Source.PUBLIC_BOOKING,
    Booking.Source.CUSTOMER_PORTAL: Customer.Source.PUBLIC_BOOKING,
    Booking.Source.RECEPTION: Customer.Source.RECEPTION,
    Booking.Source.STAFF: Customer.Source.STAFF,
    Booking.Source.IMPORT: Customer.Source.IMPORT,
}
# Status changes that are worth a customer-timeline entry (check-in etc. are not).
STATUS_ACTIVITY = {
    Booking.Status.COMPLETED: Kind.APPOINTMENT_COMPLETED,
    Booking.Status.NO_SHOW: Kind.APPOINTMENT_NO_SHOW,
}


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


DEADLOCK_SQLSTATE = "40P01"


def _constraint_conflict(error: IntegrityError | OperationalError) -> ConflictError | None:
    """The friendly error for losing a race on the no-overlap constraint, or None for any
    other failure. Two inserts that clash at the same moment can also end in a deadlock, which
    PostgreSQL resolves by aborting one of them: that one lost the slot too."""
    if OVERLAP_CONSTRAINT in str(error) or (
        getattr(error.__cause__, "sqlstate", None) == DEADLOCK_SQLSTATE
    ):
        return ConflictError("Selected slot is no longer available", code="slot_unavailable")
    return None


def _check_monthly_limit(organization) -> None:
    from subscriptions.services import enforce_plan_limit

    try:
        enforce_plan_limit(organization, "bookings")
    except ValueError as error:
        raise ConflictError(str(error), code="plan_limit") from error


def _booking_location(organization, location, *, public: bool) -> Location:
    """The location to book at: the given one (checked) or the organization's default."""
    if location is None:
        location = Location.objects.filter(organization=organization, is_default=True).first()
    if location is None or location.organization_id != organization.pk or not location.is_active:
        raise DomainError("Location not found", code="not_found")
    if public and not location.booking_enabled:
        raise DomainError("Location not found", code="not_found")
    return location


def _replayed(organization, idempotency_key, *, service, staff_profile, start_datetime):
    """The booking made earlier with this key, or None. The same key with a different request
    is refused rather than silently returning an unrelated booking."""
    if not idempotency_key:
        return None
    booking = Booking.objects.filter(
        organization=organization, idempotency_key=idempotency_key
    ).first()
    if booking is None:
        return None
    if (booking.service_id, booking.staff_id, booking.start_datetime) != (
        service.pk,
        staff_profile.pk,
        start_datetime,
    ):
        raise ConflictError(
            "This idempotency key was already used for a different booking",
            code="idempotency_key_reused",
        )
    return booking


def _unique_reference(organization, start_datetime) -> str:
    year = start_datetime.astimezone(ZoneInfo(organization.timezone)).year
    reference = Booking.generate_reference(year=year)
    while Booking.objects.filter(reference=reference).exists():
        reference = Booking.generate_reference(year=year)
    return reference


@transaction.atomic
def create_booking(
    *,
    organization,
    service,
    staff_profile,
    start_datetime,
    customer_name,
    customer_email,
    customer_phone="",
    location: Location | None = None,
    source: str = Booking.Source.STAFF,
    public: bool = False,
    customer_timezone="UTC",
    customer_notes="",
    actor=None,
    customer_user=None,
    notify: bool = True,
    customer: Customer | None = None,
    activity_kind: str | None = Kind.APPOINTMENT_BOOKED,
    idempotency_key: str = "",
    rescheduled_from: Booking | None = None,
):
    """Book an appointment, or raise ``DomainError`` (400) / ``ConflictError`` (409).

    ``location``: where (default: the organization's default location). ``public``: hold the
    time to the online-booking rules too (minimum notice, how far ahead), for bookings made by
    customers. ``source``: where the booking came from (``Booking.Source``).
    ``idempotency_key``: repeating the call with the same key returns the first booking.
    ``customer``: book for this existing CRM customer instead of looking one up by the contact
    details (reschedule). ``activity_kind``: the timeline entry to record, or None when the
    caller records its own.
    """
    # Tenant consistency is the service's job, not only the API serializer's: any caller
    # (web, admin action, integration, AI agent) gets the same refusal.
    if service.organization_id != organization.pk or staff_profile.organization_id != (
        organization.pk
    ):
        raise DomainError("Service or staff not found", code="not_found")
    if customer is not None and customer.organization_id != organization.pk:
        raise DomainError("Customer not found", code="not_found")
    if source not in Booking.Source.values:
        raise DomainError(f"Unknown booking source '{source}'", code="invalid_source")
    replay = _replayed(
        organization,
        idempotency_key,
        service=service,
        staff_profile=staff_profile,
        start_datetime=start_datetime,
    )
    if replay is not None:
        return replay
    location = _booking_location(organization, location, public=public)

    # Serialize writers to this calendar, then check the time against committed data: hours,
    # location, closures, time off, other appointments with buffers, the daily limit.
    lock_staff(staff_profile.pk)
    # A retry with the same key that waited for the lock finds the booking made meanwhile.
    replay = _replayed(
        organization,
        idempotency_key,
        service=service,
        staff_profile=staff_profile,
        start_datetime=start_datetime,
    )
    if replay is not None:
        return replay
    engine = AvailabilityService(organization, service, location=location, public=public)
    candidate = engine.validate_slot(staff_profile, start_datetime)
    _check_monthly_limit(organization)

    if customer is None:
        customer = find_or_create_customer(
            organization=organization,
            name=customer_name,
            email=customer_email,
            phone=customer_phone,
            user=customer_user,
            source=CUSTOMER_SOURCE.get(source, Customer.Source.OTHER),
            actor=actor,
        )

    offering = offering_for(staff_profile, service, location)
    price = service.price
    if offering is not None and offering.custom_price is not None:
        price = offering.custom_price
    end_datetime = candidate.end
    try:
        with transaction.atomic():  # savepoint: a constraint failure leaves the rest usable
            booking = Booking.objects.create(
                reference=_unique_reference(organization, start_datetime),
                organization=organization,
                location=location,
                source=source,
                created_by=actor if actor is not None and actor.is_authenticated else None,
                idempotency_key=idempotency_key,
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
                price_snapshot=price,
                duration_snapshot_minutes=int(
                    (end_datetime - start_datetime).total_seconds() // 60
                ),
                buffer_before_minutes=service.buffer_before_minutes,
                buffer_after_minutes=service.buffer_after_minutes,
                customer_notes=customer_notes,
                status=Booking.Status.CONFIRMED,
                rescheduled_from=rescheduled_from,
            )
    except IntegrityError as error:
        if IDEMPOTENCY_CONSTRAINT in str(error):
            # A concurrent request with the same key won: return its booking.
            return _replayed(
                organization,
                idempotency_key,
                service=service,
                staff_profile=staff_profile,
                start_datetime=start_datetime,
            )
        conflict = _constraint_conflict(error)
        if conflict is not None:
            raise conflict from error
        raise
    except OperationalError as error:
        conflict = _constraint_conflict(error)
        if conflict is not None:
            raise conflict from error
        raise

    BookingStatusHistory.objects.create(
        booking=booking,
        old_status="",
        new_status=booking.status,
        changed_by=actor,
        source=source,
        reason="Rescheduled" if rescheduled_from is not None else "",
    )
    BookingActivityLog.objects.create(
        booking=booking,
        organization=organization,
        actor=actor,
        action="booking.created",
        metadata={
            "service": str(service.id),
            "staff": str(staff_profile.id),
            "location": str(location.id),
            "source": source,
        },
    )
    record_audit(
        AuditAction.BOOKING_CREATED,
        organization=organization,
        actor=actor,
        object_type="Booking",
        object_identifier=str(booking.id),
        metadata={"reference": booking.reference, "source": source},
    )
    if activity_kind:
        record_activity(
            activity_kind,
            customer=customer,
            actor=actor,
            subject=booking,
            metadata=booking_metadata(booking),
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
def record_past_booking(
    *,
    organization,
    service,
    staff_profile,
    customer: Customer,
    start_datetime,
    status: str,
    location: Location | None = None,
    source: str = Booking.Source.IMPORT,
    reference: str = "",
    actor=None,
) -> Booking:
    """Record an appointment that already happened (imports, demo history).

    No availability check (the past can't be booked), but the time must be in the past and
    the status final, so it never occupies a calendar.
    """
    if {service.organization_id, staff_profile.organization_id, customer.organization_id} != {
        organization.pk
    }:
        raise DomainError("Service, staff or customer not found", code="not_found")
    if start_datetime >= timezone.now():
        raise DomainError("Only past appointments can be recorded", code="not_in_past")
    if status not in PAST_STATUSES:
        raise DomainError(f"A past appointment can't be '{status}'", code="invalid_status")
    location = _booking_location(organization, location, public=False)
    booking = Booking.objects.create(
        reference=reference or _unique_reference(organization, start_datetime),
        organization=organization,
        location=location,
        source=source,
        created_by=actor,
        customer=customer,
        customer_name=customer.name,
        customer_email=customer.email,
        customer_phone=customer.phone,
        service=service,
        staff=staff_profile,
        start_datetime=start_datetime,
        end_datetime=start_datetime + timedelta(minutes=service.duration_minutes),
        organization_timezone=organization.timezone,
        customer_timezone=organization.timezone,
        price_snapshot=service.price,
        duration_snapshot_minutes=service.duration_minutes,
        buffer_before_minutes=service.buffer_before_minutes,
        buffer_after_minutes=service.buffer_after_minutes,
        status=status,
    )
    BookingStatusHistory.objects.create(
        booking=booking,
        old_status="",
        new_status=status,
        changed_by=actor,
        source=source,
        reason="Imported",
    )
    return booking


def check_cancellation_deadline(booking: Booking) -> None:
    """Customers can't cancel or reschedule within the service's cancellation deadline (the
    team can, by not asking for this check)."""
    hours = booking.service.cancellation_deadline_hours
    if hours and booking.start_datetime - timezone.now() < timedelta(hours=hours):
        raise DomainError(
            f"Appointments can't be changed online less than {hours} hours before they "
            "start. Please contact us.",
            code="past_cancellation_deadline",
        )


@transaction.atomic
def cancel_booking(
    *,
    booking: Booking,
    actor=None,
    reason: str = "",
    enforce_deadline: bool = False,
    source: str = Booking.Source.STAFF,
):
    """Cancel an appointment. ``enforce_deadline``: the customer is acting (their own
    appointment), so the service's cancellation deadline applies. ``source``: the channel."""
    booking = lock_booking(booking)
    if booking.status == Booking.Status.CANCELLED:
        return booking
    if Booking.Status.CANCELLED not in ALLOWED_TRANSITIONS[booking.status]:
        raise ConflictError(
            f"A {booking.get_status_display().lower()} appointment cannot be cancelled",
            code="invalid_transition",
        )
    if enforce_deadline:
        check_cancellation_deadline(booking)
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
        source=source,
        reason=reason[:255],
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
    record_activity(
        Kind.APPOINTMENT_CANCELLED,
        customer=booking.customer,
        actor=actor,
        subject=booking,
        metadata={**booking_metadata(booking), "from_status": old_status},
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


def check_timing(booking: Booking, new_status: str, now) -> None:
    """Statuses that only make sense around the appointment's time: check-in, starting and
    completing from an hour before it starts; no-show once it has started."""
    early = booking.start_datetime - CHECK_IN_EARLIEST
    if new_status in ARRIVAL_STATUSES and now < early:
        raise DomainError(
            "An appointment can be checked in, started or completed from an hour before it "
            "starts.",
            code="too_early",
        )
    if new_status == Booking.Status.NO_SHOW and now < booking.start_datetime:
        raise DomainError(
            "An appointment can only be marked as a no-show once it has started.",
            code="too_early",
        )


@transaction.atomic
def change_booking_status(
    *,
    booking: Booking,
    new_status: str,
    actor=None,
    note: str = "",
    reason: str = "",
    source: str = Booking.Source.STAFF,
):
    """Move an appointment along its lifecycle (``ALLOWED_TRANSITIONS``), or raise 409
    ``invalid_transition`` / 400 ``too_early``. Check-in and completion are timestamped;
    completed and no-show appointments go on the customer's timeline."""
    if new_status not in Booking.Status.values:
        raise DomainError(f"Unknown status '{new_status}'", code="invalid_status")
    if new_status == Booking.Status.CANCELLED:
        return cancel_booking(booking=booking, actor=actor, reason=reason or note, source=source)

    booking = lock_booking(booking)
    if new_status not in ALLOWED_TRANSITIONS[booking.status]:
        raise ConflictError(
            f"A {booking.get_status_display().lower()} appointment can't become "
            f"{Booking.Status(new_status).label.lower()}",
            code="invalid_transition",
        )
    now = timezone.now()
    check_timing(booking, new_status, now)
    old_status = booking.status
    booking.status = new_status
    fields = ["status", "updated_at"]
    if new_status == Booking.Status.CHECKED_IN:
        booking.checked_in_at = now
        fields.append("checked_in_at")
    if new_status == Booking.Status.COMPLETED:
        booking.completed_at = now
        fields.append("completed_at")
    booking.save(update_fields=fields)
    BookingStatusHistory.objects.create(
        booking=booking,
        old_status=old_status,
        new_status=new_status,
        changed_by=actor,
        source=source,
        reason=reason[:255],
        note=note,
    )
    record_audit(
        AuditAction.BOOKING_STATUS_CHANGED,
        organization=booking.organization,
        actor=actor,
        object_type="Booking",
        object_identifier=str(booking.id),
        metadata={"from": old_status, "to": new_status, "source": source},
    )
    outcome = STATUS_ACTIVITY.get(new_status)
    if outcome:
        record_activity(
            outcome,
            customer=booking.customer,
            actor=actor,
            subject=booking,
            metadata=booking_metadata(booking),
        )
    return booking


def check_in(*, booking: Booking, actor=None, source: str = Booking.Source.STAFF) -> Booking:
    """The customer has arrived (confirmed -> checked in)."""
    return change_booking_status(
        booking=booking, new_status=Booking.Status.CHECKED_IN, actor=actor, source=source
    )


def check_out(*, booking: Booking, actor=None, source: str = Booking.Source.STAFF) -> Booking:
    """The appointment is over (checked in or in progress -> completed)."""
    return change_booking_status(
        booking=booking, new_status=Booking.Status.COMPLETED, actor=actor, source=source
    )


@transaction.atomic
def reschedule_booking(
    *,
    booking: Booking,
    new_start,
    actor=None,
    public: bool = False,
    source: str = Booking.Source.STAFF,
) -> Booking:
    """Move an appointment to ``new_start``: one transaction, all-or-nothing.

    The old appointment is closed as cancelled ("Rescheduled") and a new one is created at the
    same location with the same provider, linked via ``rescheduled_from``. The new time goes
    through the same checks as a new booking (the old appointment no longer blocks it). If it
    is refused, nothing changes. ``public``: the customer is acting, so the online-booking
    rules and the cancellation deadline apply.
    """
    lock_staff(booking.staff_id)
    booking = lock_booking(booking)
    if booking.status not in RESCHEDULABLE_STATUSES:
        raise ConflictError(
            f"A {booking.get_status_display().lower()} appointment cannot be rescheduled",
            code="invalid_transition",
        )
    if public:
        check_cancellation_deadline(booking)

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
        source=source,
        reason="Rescheduled",
        note="Rescheduled",
    )

    new_booking = create_booking(
        organization=booking.organization,
        service=booking.service,
        staff_profile=booking.staff,
        location=booking.location,
        source=booking.source,
        public=public,
        customer_name=booking.customer_name,
        customer_email=booking.customer_email,
        customer_phone=booking.customer_phone,
        start_datetime=new_start,
        customer_timezone=booking.customer_timezone,
        customer_notes=booking.customer_notes,
        actor=actor,
        customer_user=booking.customer.user if booking.customer else None,
        # Same CRM customer, even if their email changed since the booking was made.
        customer=booking.customer,
        activity_kind=None,
        rescheduled_from=booking,
    )
    record_audit(
        AuditAction.BOOKING_RESCHEDULED,
        organization=booking.organization,
        actor=actor,
        object_type="Booking",
        object_identifier=str(new_booking.id),
        metadata={"from_booking": str(booking.id)},
    )
    record_activity(
        Kind.APPOINTMENT_RESCHEDULED,
        customer=new_booking.customer,
        actor=actor,
        subject=new_booking,
        metadata={
            **booking_metadata(new_booking),
            "from_reference": booking.reference,
            "from_start": booking.start_datetime.isoformat(),
        },
    )
    return new_booking
