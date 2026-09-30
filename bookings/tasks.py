from bookings.models import Booking
from bookings.waitlist import notify_matching_entries
from config.celery import app


@app.task
def notify_waitlist(*, booking_id, organization_id):
    """Offer the time freed by a cancelled or moved appointment to the waitlist. The tenant
    is explicit: the booking must belong to the organization the caller named."""
    booking = (
        Booking.objects.select_related("organization", "service", "staff", "location")
        .filter(pk=booking_id, organization_id=organization_id)
        .first()
    )
    if booking is None:
        return 0
    return len(notify_matching_entries(booking))
