# M2.2: copy the legacy Customer.tags JSON strings into crm.Tag / crm.CustomerTag.
# One Tag per distinct slug per organization; the first spelling seen becomes its name.
# The JSON field is left untouched (read-only until M11.3). Reversing removes all tags.

from django.db import migrations
from django.utils.text import slugify


def backfill(apps, schema_editor):
    Customer = apps.get_model("bookings", "Customer")
    Tag = apps.get_model("crm", "Tag")
    CustomerTag = apps.get_model("crm", "CustomerTag")
    for customer in Customer.objects.exclude(tags=[]).iterator():
        values = customer.tags if isinstance(customer.tags, list) else []
        for value in values:
            name = str(value).strip()[:60]
            slug = slugify(name)[:80]
            if not slug:
                continue
            tag, _ = Tag.objects.get_or_create(
                organization_id=customer.organization_id, slug=slug, defaults={"name": name}
            )
            CustomerTag.objects.get_or_create(
                customer=customer, tag=tag, defaults={"organization_id": customer.organization_id}
            )


def remove_all(apps, schema_editor):
    apps.get_model("crm", "CustomerTag").objects.all().delete()
    apps.get_model("crm", "Tag").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0001_initial"),
        ("bookings", "0005_customer_tag_set"),
    ]

    operations = [
        migrations.RunPython(backfill, remove_all),
    ]
