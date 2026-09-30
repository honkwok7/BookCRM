"""Validate the organization's time zone (as a location's already is).

An unknown zone would make ``ZoneInfo(organization.timezone)`` fail in booking references and
the monthly plan limit, so any organization that has one is reset to UTC first. Reversing drops
the validator; reset zones stay UTC.
"""

import zoneinfo

from django.db import migrations, models

import core.validators


def reset_unknown_zones(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    known = zoneinfo.available_timezones()
    for organization in Organization.objects.only("pk", "timezone"):
        if organization.timezone not in known:
            Organization.objects.filter(pk=organization.pk).update(timezone="UTC")


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0003_booking_page_branding"),
    ]

    operations = [
        migrations.RunPython(reset_unknown_zones, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="organization",
            name="timezone",
            field=models.CharField(
                default="UTC", max_length=64, validators=[core.validators.validate_timezone]
            ),
        ),
    ]
