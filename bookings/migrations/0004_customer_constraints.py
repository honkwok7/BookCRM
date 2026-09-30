# M2.1 step 3 of 3: swap constraints and drop the never-maintained counters.
# - unique (organization, email) becomes case-insensitive and ignores blank emails;
# - an email or a phone is required (anonymized customers are exempt);
# - total_bookings / no_show_count / last_appointment were never updated (always 0/empty);
#   crm.selectors.customer_stats computes them from bookings instead.
# Customers whose emails differ only in case (allowed by the old, case-sensitive rule) are
# merged first, so the new constraint can be added: see merge_case_duplicates.

import django.db.models.functions.text
from django.db import migrations, models
from django.db.models.functions import Lower

# Filled from a later duplicate when the kept customer has them blank.
FILL_FIELDS = ("first_name", "last_name", "name", "phone", "notes")


def merge_case_duplicates(apps, schema_editor):
    """Per organization, customers whose emails are equal ignoring case become one: the
    oldest is kept, takes the others' appointments and fills its blank details from them, and
    the others are deleted. Only appointments refer to customers at this point (the CRM tables
    come later). One-way: reversing leaves the merged customer."""
    Customer = apps.get_model("bookings", "Customer")
    Booking = apps.get_model("bookings", "Booking")
    fields = {field.name for field in Customer._meta.get_fields()}
    keepers = {}
    rows = (
        Customer.objects.exclude(email="")
        .annotate(email_key=Lower("email"))
        .order_by("created_at", "pk")
    )
    for customer in rows:
        key = (customer.organization_id, customer.email_key)
        keeper = keepers.get(key)
        if keeper is None:
            keepers[key] = customer
            continue
        Booking.objects.filter(customer=customer).update(customer=keeper)
        changed = [
            name
            for name in FILL_FIELDS
            if name in fields and not getattr(keeper, name) and getattr(customer, name)
        ]
        for name in changed:
            setattr(keeper, name, getattr(customer, name))
        if changed:
            keeper.save(update_fields=changed)
        customer.delete()
    if schema_editor.connection.vendor == "postgresql":
        # Run the deferred foreign-key checks now: the ALTER TABLEs that follow refuse to run
        # with pending trigger events.
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0003_customer_split_names"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="customer",
            unique_together=set(),
        ),
        migrations.RunPython(merge_case_duplicates, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="customer",
            index=models.Index(
                fields=["organization", "last_name", "first_name"],
                name="bookings_cu_organiz_6f5c2b_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="customer",
            index=models.Index(
                fields=["organization", "phone"], name="bookings_cu_organiz_fe228e_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="customer",
            index=models.Index(
                fields=["organization", "status"], name="bookings_cu_organiz_753804_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="customer",
            constraint=models.UniqueConstraint(
                models.F("organization"),
                django.db.models.functions.text.Lower("email"),
                condition=models.Q(("email", ""), _negated=True),
                name="customer_unique_email_per_org",
            ),
        ),
        migrations.AddConstraint(
            model_name="customer",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("email", ""), _negated=True),
                    models.Q(("phone", ""), _negated=True),
                    ("status", "anonymized"),
                    _connector="OR",
                ),
                name="customer_email_or_phone_required",
            ),
        ),
        migrations.RemoveField(
            model_name="customer",
            name="last_appointment",
        ),
        migrations.RemoveField(
            model_name="customer",
            name="no_show_count",
        ),
        migrations.RemoveField(
            model_name="customer",
            name="total_bookings",
        ),
    ]
