# M2.3: build timeline history for data that existed before the activity log.
# - customer_created for every customer without one (at the customer's creation time);
# - per booking with a customer: booked (or rescheduled, if it replaced another booking) at
#   its creation time, then cancelled / completed / no-show at its last change. A booking
#   cancelled because it was rescheduled gets no "cancelled" entry: its successor records the
#   reschedule. Idempotent: bookings that already have entries are skipped.

from django.db import migrations

BOOKING = "bookings.booking"
OUTCOMES = {
    "cancelled": "appointment_cancelled",
    "completed": "appointment_completed",
    "no_show": "appointment_no_show",
}


def backfill(apps, schema_editor):
    Customer = apps.get_model("bookings", "Customer")
    Booking = apps.get_model("bookings", "Booking")
    Activity = apps.get_model("crm", "CustomerActivity")

    for customer in Customer.objects.filter(activities__isnull=True).iterator():
        Activity.objects.create(
            organization_id=customer.organization_id,
            customer=customer,
            kind="customer_created",
            metadata={"source": customer.source, "backfilled": True},
            occurred_at=customer.created_at,
        )

    done = set(Activity.objects.filter(subject_type=BOOKING).values_list("subject_id", flat=True))
    rescheduled_away = set(
        Booking.objects.exclude(rescheduled_from=None).values_list("rescheduled_from", flat=True)
    )
    entries = []
    for booking in Booking.objects.exclude(customer=None).iterator():
        if str(booking.pk) in done:
            continue
        base = {
            "organization_id": booking.organization_id,
            "customer_id": booking.customer_id,
            "subject_type": BOOKING,
            "subject_id": str(booking.pk),
            "metadata": {
                "reference": booking.reference,
                "service": str(booking.service_id),
                "staff": str(booking.staff_id),
                "start": booking.start_datetime.isoformat(),
                "backfilled": True,
            },
        }
        first = "appointment_rescheduled" if booking.rescheduled_from_id else "appointment_booked"
        entries.append(Activity(kind=first, occurred_at=booking.created_at, **base))
        outcome = OUTCOMES.get(booking.status)
        if outcome and booking.pk not in rescheduled_away:
            when = booking.cancelled_at or booking.updated_at
            entries.append(Activity(kind=outcome, occurred_at=when, **base))
    Activity.objects.bulk_create(entries, batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0003_notes_and_activity"),
        ("bookings", "0005_customer_tag_set"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
