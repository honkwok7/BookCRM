"""The waitlist (M4.7): joining, closing, and telling waiting customers when a time frees up.

- ``join_waitlist``: the only way to create an entry (public page, reception screen). It
  links the entry to the CRM customer (found or created like a booking's customer), and
  joining again for the same service updates the waiting entry instead of adding another.
- When an appointment is cancelled (or moved away), ``schedule_matching`` queues a task after
  the transaction commits. ``notify_matching_entries`` finds the waiting entries that the
  freed time suits and emails them, first come first served, at most
  ``NOTIFY_LIMIT`` per freed time. Each entry is notified once (it becomes "notified").
- Matching: same service; the entry's location (or any); its provider (or any, as long as
  the freed provider offers the service there); the freed date inside its date range; the
  freed start in its time of day; not expired.

Nothing here reveals one customer's entry to another: the email names only the service, the
time and the place, with a link to the booking page.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from functools import partial
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from bookings.models import Booking, Customer, WaitlistEntry
from core.audit import AuditAction, record_audit
from core.exceptions import DomainError
from crm.services import find_or_create_customer
from notifications.services import queue_waitlist_notification

logger = logging.getLogger(__name__)

NOTIFY_LIMIT = 5
# At most one waiting entry per customer and service (see ``WaitlistEntry.Meta``).
ONE_WAITING_CONSTRAINT = "waitlist_one_waiting_entry_per_customer"
DEFAULT_WAIT = timedelta(days=60)
TIME_OF_DAY_HOURS = {
    WaitlistEntry.TimeOfDay.MORNING: (0, 12),
    WaitlistEntry.TimeOfDay.AFTERNOON: (12, 17),
    WaitlistEntry.TimeOfDay.EVENING: (17, 24),
}
CUSTOMER_SOURCE = {
    Booking.Source.PUBLIC_BOOKING: Customer.Source.PUBLIC_BOOKING,
    Booking.Source.RECEPTION: Customer.Source.RECEPTION,
    Booking.Source.STAFF: Customer.Source.STAFF,
}


def _zone(organization, location) -> ZoneInfo:
    try:
        return ZoneInfo((location or organization).timezone)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


@transaction.atomic
def join_waitlist(
    *,
    organization,
    service,
    customer_name: str,
    customer_email: str,
    customer_phone: str = "",
    location=None,
    preferred_staff=None,
    preferred_start_date=None,
    preferred_end_date=None,
    time_of_day: str = WaitlistEntry.TimeOfDay.ANY,
    source: str = Booking.Source.RECEPTION,
    actor=None,
    customer: Customer | None = None,
    customer_user=None,
) -> WaitlistEntry:
    """Add someone to the waitlist for ``service`` (or update their waiting entry)."""
    for item in (service, location, preferred_staff, customer):
        if item is not None and item.organization_id != organization.pk:
            raise DomainError("Not found", code="not_found")
    if not service.is_active or service.is_archived:
        raise DomainError("This service can't be booked", code="not_found")
    if time_of_day not in WaitlistEntry.TimeOfDay.values:
        raise DomainError("Unknown time of day", code="invalid_time_of_day")
    _check_preferences(service, location, preferred_staff)
    customer_email = customer_email.strip()
    if not customer_email:
        raise DomainError(
            "An email address is needed to tell you when a time frees up.", code="email_required"
        )
    zone = _zone(organization, location)
    today = timezone.now().astimezone(zone).date()
    if preferred_start_date and preferred_end_date and preferred_end_date < preferred_start_date:
        raise DomainError("The last date must be after the first date.", code="invalid_dates")
    if preferred_end_date and preferred_end_date < today:
        raise DomainError("The dates are in the past.", code="invalid_dates")
    expires_at = timezone.now() + DEFAULT_WAIT
    if preferred_end_date:
        expires_at = datetime.combine(preferred_end_date + timedelta(days=1), time.min, zone)

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
    fields = {
        "customer_name": customer_name.strip(),
        "customer_email": customer_email,
        "customer_phone": customer_phone.strip(),
        "location": location,
        "preferred_staff": preferred_staff,
        "preferred_start_date": preferred_start_date,
        "preferred_end_date": preferred_end_date,
        "time_of_day": time_of_day,
        "expires_at": expires_at,
    }
    entry = _waiting_entry(organization, service, customer)
    action = AuditAction.WAITLIST_ENTRY_UPDATED
    if entry is None:
        try:
            with transaction.atomic():  # savepoint: a lost race leaves the rest usable
                entry = WaitlistEntry.objects.create(
                    organization=organization,
                    service=service,
                    customer=customer,
                    source=source,
                    **fields,
                )
            action = AuditAction.WAITLIST_ENTRY_CREATED
        except IntegrityError as error:
            if ONE_WAITING_CONSTRAINT not in str(error):
                raise
            # A concurrent join for the same customer and service won: update its entry.
            entry = _waiting_entry(organization, service, customer)
            if entry is None:
                raise
    if action == AuditAction.WAITLIST_ENTRY_UPDATED:
        for name, value in fields.items():
            setattr(entry, name, value)
        entry.save()
    record_audit(
        action,
        organization=organization,
        actor=actor if actor is not None and actor.is_authenticated else None,
        object_type="WaitlistEntry",
        object_identifier=str(entry.pk),
        metadata={"service": str(service.pk), "source": source},
    )
    return entry


def _waiting_entry(organization, service, customer) -> WaitlistEntry | None:
    return (
        WaitlistEntry.objects.select_for_update()
        .filter(
            organization=organization,
            service=service,
            customer=customer,
            status=WaitlistEntry.Status.WAITING,
        )
        .first()
    )


def _check_preferences(service, location, preferred_staff) -> None:
    """Refuse preferences that no freed time could ever match: a provider who doesn't offer
    the service (at the location), or a location where nobody does."""
    from staff.selectors import list_providers_for, provides

    if preferred_staff is not None:
        if not provides(preferred_staff, service, location):
            where = f" at {location.name}" if location is not None else ""
            raise DomainError(
                f"{preferred_staff.public_name} doesn't offer {service.name}{where}.",
                code="invalid_preferences",
            )
    elif location is not None and not list_providers_for(service, location).exists():
        raise DomainError(
            f"{service.name} isn't offered at {location.name}.", code="invalid_preferences"
        )


@transaction.atomic
def close_entry(*, entry: WaitlistEntry, actor=None) -> WaitlistEntry:
    entry = WaitlistEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.status != WaitlistEntry.Status.CLOSED:
        entry.status = WaitlistEntry.Status.CLOSED
        entry.save(update_fields=["status", "updated_at"])
        record_audit(
            AuditAction.WAITLIST_ENTRY_UPDATED,
            organization=entry.organization,
            actor=actor,
            object_type="WaitlistEntry",
            object_identifier=str(entry.pk),
            metadata={"status": entry.status},
        )
    return entry


# -- Matching a freed time --------------------------------------------------------------------


def matching_entries(booking: Booking, *, now=None) -> list[WaitlistEntry]:
    """Waiting entries that the time freed by ``booking`` suits, first come first served."""
    from staff.selectors import provides

    now = now or timezone.now()
    if booking.start_datetime <= now:
        return []
    zone = _zone(booking.organization, booking.location)
    local = booking.start_datetime.astimezone(zone)
    candidates = (
        WaitlistEntry.objects.filter(
            organization=booking.organization,
            service=booking.service,
            status=WaitlistEntry.Status.WAITING,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .filter(Q(location__isnull=True) | Q(location=booking.location))
        .filter(Q(preferred_staff__isnull=True) | Q(preferred_staff=booking.staff))
        .filter(Q(preferred_start_date__isnull=True) | Q(preferred_start_date__lte=local.date()))
        .filter(Q(preferred_end_date__isnull=True) | Q(preferred_end_date__gte=local.date()))
        .order_by("created_at")
    )
    matches = []
    for entry in candidates:
        hours = TIME_OF_DAY_HOURS.get(entry.time_of_day)
        if hours and not hours[0] <= local.hour < hours[1]:
            continue
        matches.append(entry)
    if matches and not provides(booking.staff, booking.service, booking.location):
        return []  # the freed provider no longer offers it: nothing to offer
    if matches and _taken_again(booking):
        return []  # booked again before the task ran: nothing was freed after all
    return matches


def _taken_again(booking: Booking) -> bool:
    """Does another active appointment of the provider now overlap the freed time?"""
    from bookings.services import ACTIVE_BOOKING_STATUSES

    return (
        Booking.objects.filter(
            staff_id=booking.staff_id,
            status__in=ACTIVE_BOOKING_STATUSES,
            start_datetime__lt=booking.end_datetime,
            end_datetime__gt=booking.start_datetime,
        )
        .exclude(pk=booking.pk)
        .exists()
    )


@transaction.atomic
def notify_matching_entries(booking: Booking) -> list[WaitlistEntry]:
    """Email up to ``NOTIFY_LIMIT`` matching entries about the time ``booking`` freed."""
    notified = []
    for entry in matching_entries(booking)[:NOTIFY_LIMIT]:
        # Claim the entry: a concurrent run (another cancellation) can't notify it twice.
        claimed = WaitlistEntry.objects.filter(
            pk=entry.pk, status=WaitlistEntry.Status.WAITING
        ).update(status=WaitlistEntry.Status.NOTIFIED, notified_at=timezone.now())
        if not claimed:
            continue
        queue_waitlist_notification(entry=entry, booking=booking)
        notified.append(entry)
    return notified


def _enqueue_matching(booking_id: str, organization_id: str) -> None:
    from bookings.tasks import notify_waitlist

    try:
        notify_waitlist.delay(booking_id=booking_id, organization_id=organization_id)
    except Exception:
        # Broker unavailable: the freed time simply isn't offered to the waitlist.
        logger.warning("Could not enqueue waitlist matching for %s", booking_id, exc_info=True)


def schedule_matching(booking: Booking) -> None:
    """After the current transaction commits, look for waitlist entries for the time
    ``booking`` no longer occupies."""
    if booking.start_datetime <= timezone.now():
        return
    transaction.on_commit(
        partial(
            _enqueue_matching,
            booking_id=str(booking.pk),
            organization_id=str(booking.organization_id),
        )
    )
