# Review fix F4: one account per mailbox. Emails are stored lower-case and a unique index on
# LOWER(email) makes the database, not only the serializer, refuse "Sam@x" next to "sam@x".
# If existing accounts differ only by case, the migration stops and lists them: merge or
# rename them by hand first (deleting an account is a product decision, not a migration's).

import django.db.models.functions.text
from django.db import migrations, models
from django.db.models import Count
from django.db.models.functions import Lower


def lowercase_emails(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    clashes = list(
        User.objects.annotate(normalized=Lower("email"))
        .values("normalized")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("normalized", flat=True)
    )
    if clashes:
        raise RuntimeError(
            "Accounts whose emails differ only by case must be resolved before this migration: "
            + ", ".join(sorted(clashes))
        )
    for user in User.objects.exclude(email=Lower("email")):
        user.email = user.email.lower()
        user.save(update_fields=["email"])


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_remove_user_role"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(lowercase_emails, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("email"),
                name="user_email_unique_ignoring_case",
            ),
        ),
    ]
