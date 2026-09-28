# M1.6: replaced by ``staff.StaffProfile`` and ``scheduling.WeeklyAvailability``.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("specialists", "0003_specialist_user_fk"),
        ("appointments", "0003_delete_legacy_appointment"),
    ]

    operations = [
        migrations.DeleteModel(name="WorkingHour"),
        migrations.DeleteModel(name="Specialist"),
    ]
