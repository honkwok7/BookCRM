# Review fix F1: Customer.notes was internal free text readable by anyone who could see
# the customer, bypassing customers.notes.private. Move it into an internal CustomerNote
# (same visibility rules as every other internal note) and blank the legacy column.
# Irreversible by design: reversing would copy notes back into the unprotected field.

from django.db import migrations


def move_notes(apps, schema_editor):
    Customer = apps.get_model("bookings", "Customer")
    CustomerNote = apps.get_model("crm", "CustomerNote")
    for customer in Customer.objects.exclude(notes="").iterator():
        CustomerNote.objects.create(
            organization_id=customer.organization_id,
            customer=customer,
            visibility="internal",
            note_type="general",
            content=customer.notes.strip(),
        )
        customer.notes = ""
        customer.save(update_fields=["notes"])


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0004_backfill_activity"),
        ("bookings", "0007_customer_trigram_indexes"),
    ]

    operations = [
        migrations.RunPython(move_notes, migrations.RunPython.noop),
    ]
