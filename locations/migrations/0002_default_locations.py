"""Give every existing organization its default "Main" location, from the organization's
address, time zone, phone and email. New organizations get theirs from locations.signals.

Mirrors ``locations.services.default_location_fields`` (migrations can't import app code that
may change later). Reversing deletes only the locations this migration could have created.
"""

import zoneinfo

from django.db import migrations
from django.utils.text import slugify


def create_default_locations(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    Location = apps.get_model("locations", "Location")
    known_zones = zoneinfo.available_timezones()
    for organization in Organization.objects.all().iterator():
        if Location.objects.filter(organization=organization, is_default=True).exists():
            continue
        lines = [line.strip() for line in (organization.address or "").splitlines() if line.strip()]
        timezone = organization.timezone if organization.timezone in known_zones else "UTC"
        Location.objects.create(
            organization=organization,
            name="Main",
            slug=slugify("Main"),
            is_default=True,
            is_active=True,
            address_line1=lines[0][:255] if lines else "",
            address_line2=", ".join(lines[1:])[:255],
            timezone=timezone,
            phone=organization.phone or "",
            email=organization.email or "",
        )


def remove_default_locations(apps, schema_editor):
    Location = apps.get_model("locations", "Location")
    Location.objects.filter(is_default=True, slug="main").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("locations", "0001_initial"),
        ("organizations", "0001_initial"),
    ]

    operations = [migrations.RunPython(create_default_locations, remove_default_locations)]
