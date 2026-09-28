# M2.1 step 2 of 3: split the legacy ``name`` into first/last name.
# Rule: the last whitespace-separated token is the last name ("Mary Ann Smith" -> "Mary Ann",
# "Smith"); a single token becomes the first name. ``name`` itself is kept and stays in sync.

from django.db import migrations


def split_names(apps, schema_editor):
    Customer = apps.get_model("bookings", "Customer")
    for customer in Customer.objects.filter(first_name="", last_name="").exclude(name=""):
        parts = customer.name.split()
        if len(parts) < 2:
            customer.first_name, customer.last_name = parts[0], ""
        else:
            customer.first_name, customer.last_name = " ".join(parts[:-1]), parts[-1]
        customer.save(update_fields=["first_name", "last_name"])


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0002_customer_crm_fields"),
    ]

    operations = [
        migrations.RunPython(split_names, migrations.RunPython.noop),
    ]
