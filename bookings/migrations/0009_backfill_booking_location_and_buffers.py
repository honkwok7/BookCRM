"""Existing appointments were made when each organization had one location: place them at its
default location and snapshot their service's buffers. Reversing is a no-op."""

from django.db import migrations
from django.db.models import OuterRef, Subquery


def backfill(apps, schema_editor):
    Booking = apps.get_model("bookings", "Booking")
    Location = apps.get_model("locations", "Location")
    Service = apps.get_model("services", "Service")
    default_location = Location.objects.filter(
        organization_id=OuterRef("organization_id"), is_default=True
    ).values("pk")[:1]
    Booking.objects.filter(location__isnull=True).update(location=Subquery(default_location))
    service = Service.objects.filter(pk=OuterRef("service_id"))
    Booking.objects.update(
        buffer_before_minutes=Subquery(service.values("buffer_before_minutes")[:1]),
        buffer_after_minutes=Subquery(service.values("buffer_after_minutes")[:1]),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0008_booking_source_location_idempotency"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
