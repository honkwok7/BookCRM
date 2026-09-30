import logging
from functools import partial

from django.db import transaction

from notifications.models import NotificationLog
from notifications.tasks import send_templated_email

logger = logging.getLogger(__name__)


def _dispatch_email(
    *, notification_log_id: str, organization_id: str, subject: str, template_base: str
) -> None:
    try:
        send_templated_email.delay(
            notification_log_id=notification_log_id,
            organization_id=organization_id,
            subject=subject,
            template_base=template_base,
        )
    except Exception:
        # Broker unavailable: leave the log PENDING instead of sending synchronously.
        logger.warning("Could not enqueue notification %s", notification_log_id, exc_info=True)


def queue_booking_notification(
    *,
    booking,
    notification_type: str,
    subject: str,
    template_base: str,
    recipient_email: str,
    recipient_user=None,
):
    log = NotificationLog.objects.create(
        organization=booking.organization,
        recipient=recipient_user,
        recipient_email=recipient_email,
        notification_type=notification_type,
        related_booking=booking,
        status=NotificationLog.Status.PENDING,
    )
    # Only ids cross the Celery boundary, and only once the booking transaction has committed.
    transaction.on_commit(
        partial(
            _dispatch_email,
            notification_log_id=str(log.id),
            organization_id=str(booking.organization_id),
            subject=subject,
            template_base=template_base,
        )
    )
    return log


def queue_waitlist_notification(*, entry, booking):
    """Tell a waitlisted customer that ``booking``'s time has freed up (after commit)."""
    log = NotificationLog.objects.create(
        organization=booking.organization,
        recipient=entry.customer.user if entry.customer else None,
        recipient_email=entry.customer_email,
        notification_type="waitlist_slot_available",
        related_booking=booking,
        related_waitlist_entry=entry,
        status=NotificationLog.Status.PENDING,
    )
    transaction.on_commit(
        partial(
            _dispatch_email,
            notification_log_id=str(log.id),
            organization_id=str(booking.organization_id),
            subject=f"A time is available: {booking.service.name}",
            template_base="waitlist_slot_available",
        )
    )
    return log
