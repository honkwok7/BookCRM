# M1.6: the legacy booking domain is replaced by ``bookings.Booking`` (no production data).

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("appointments", "0002_appointment_lifecycle_fields"),
    ]

    operations = [
        migrations.DeleteModel(name="Appointment"),
    ]
