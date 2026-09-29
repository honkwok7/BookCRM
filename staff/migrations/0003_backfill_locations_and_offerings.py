"""Backfill M3.2 assignments from what existed before:

- every staff profile works at its organization's default location;
- every ``Service.assigned_staff_members`` link becomes an offering at all the person's
  locations (no custom duration or price).

Reversing is a no-op: backfilled rows can't be told apart from ones created later, and
reversing 0002 drops the offering table and the location links anyway.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    StaffProfile = apps.get_model("staff", "StaffProfile")
    StaffServiceOffering = apps.get_model("staff", "StaffServiceOffering")
    Location = apps.get_model("locations", "Location")
    Service = apps.get_model("services", "Service")

    defaults = dict(Location.objects.filter(is_default=True).values_list("organization_id", "pk"))
    for staff in StaffProfile.objects.all().iterator():
        default = defaults.get(staff.organization_id)
        if default is not None and not staff.locations.exists():
            staff.locations.add(default)

    Assignment = Service.assigned_staff_members.through
    existing = set(
        StaffServiceOffering.objects.filter(location__isnull=True).values_list(
            "staff_id", "service_id"
        )
    )
    rows = []
    for link in Assignment.objects.select_related("service", "staffprofile").iterator():
        service, staff = link.service, link.staffprofile
        if service.organization_id != staff.organization_id:
            continue  # a cross-tenant link would never have been valid; don't carry it over
        if (staff.pk, service.pk) in existing:
            continue
        rows.append(
            StaffServiceOffering(
                organization_id=service.organization_id,
                staff_id=staff.pk,
                service_id=service.pk,
                location=None,
            )
        )
    StaffServiceOffering.objects.bulk_create(rows)


class Migration(migrations.Migration):
    dependencies = [("staff", "0002_locations_and_offerings")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
