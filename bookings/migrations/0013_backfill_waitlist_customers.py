"""Link existing waitlist entries to the CRM customer with the same email in the same
organization (case-insensitive). Entries without a match keep only their contact details.
Reversing is a no-op."""

from django.db import migrations
from django.db.models.functions import Lower


def backfill(apps, schema_editor):
    WaitlistEntry = apps.get_model("bookings", "WaitlistEntry")
    Customer = apps.get_model("bookings", "Customer")
    for entry in WaitlistEntry.objects.filter(customer__isnull=True).exclude(customer_email=""):
        customer = (
            Customer.objects.annotate(email_lower=Lower("email"))
            .filter(organization_id=entry.organization_id, email_lower=entry.customer_email.lower())
            .exclude(status="anonymized")
            .first()
        )
        if customer is not None:
            WaitlistEntry.objects.filter(pk=entry.pk).update(customer=customer)


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0012_waitlist_enhancement"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
