"""At most one waiting entry per customer and service.

Existing duplicates (possible before this constraint, when two joins raced) are resolved
first: the oldest waiting entry keeps its place in the queue and the later ones are closed.
Reversing drops the constraint; closed duplicates stay closed.
"""

from django.db import migrations, models


def close_duplicates(apps, schema_editor):
    WaitlistEntry = apps.get_model("bookings", "WaitlistEntry")
    seen = set()
    waiting = WaitlistEntry.objects.filter(status="waiting", customer__isnull=False).order_by(
        "created_at", "pk"
    )
    for entry in waiting.only("pk", "organization_id", "service_id", "customer_id"):
        key = (entry.organization_id, entry.service_id, entry.customer_id)
        if key in seen:
            WaitlistEntry.objects.filter(pk=entry.pk).update(status="closed")
        else:
            seen.add(key)


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0013_backfill_waitlist_customers"),
    ]

    operations = [
        migrations.RunPython(close_duplicates, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="waitlistentry",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "waiting")),
                fields=("organization", "service", "customer"),
                name="waitlist_one_waiting_entry_per_customer",
            ),
        ),
    ]
