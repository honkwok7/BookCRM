# M2.1 step 3 of 3: swap constraints and drop the never-maintained counters.
# - unique (organization, email) becomes case-insensitive and ignores blank emails;
# - an email or a phone is required (anonymized customers are exempt);
# - total_bookings / no_show_count / last_appointment were never updated (always 0/empty);
#   crm.selectors.customer_stats computes them from bookings instead.
# If existing data has two customers whose emails differ only in case, the unique constraint
# fails here: merge them first (crm.services.merge_customers).

import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0003_customer_split_names"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="customer",
            unique_together=set(),
        ),
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
