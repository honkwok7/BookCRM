"""Existing weekly hours applied at the organization's only location at the time: set them to
its default location, so adding a second location doesn't silently make everyone available
there too. Reversing is a no-op (rows can't be told apart from ones set later)."""

from django.db import migrations


def backfill(apps, schema_editor):
    WeeklyAvailability = apps.get_model("scheduling", "WeeklyAvailability")
    Location = apps.get_model("locations", "Location")
    defaults = dict(Location.objects.filter(is_default=True).values_list("organization_id", "pk"))
    for organization_id, location_id in defaults.items():
        WeeklyAvailability.objects.filter(
            organization_id=organization_id, location__isnull=True
        ).update(location_id=location_id)


class Migration(migrations.Migration):
    dependencies = [
        ("scheduling", "0002_weekly_availability_location"),
        ("locations", "0002_default_locations"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
