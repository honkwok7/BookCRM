# M2.4: trigram indexes for customer search (PostgreSQL only; other databases skip them and
# fall back to sequential scans, which is fine for development).
#
# Django's `icontains` compiles to UPPER(col) LIKE UPPER(%s), so the name/email indexes are on
# UPPER(col); `phone_search__contains` compiles to a plain LIKE. pg_trgm is a trusted
# extension (PostgreSQL 13+): the database owner can create it without superuser rights.

from django.db import migrations

INDEXED = {
    "customer_first_name_trgm": 'UPPER("first_name")',
    "customer_last_name_trgm": 'UPPER("last_name")',
    "customer_preferred_name_trgm": 'UPPER("preferred_name")',
    "customer_email_trgm": 'UPPER("email")',
    "customer_phone_search_trgm": '"phone_search"',
}


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, expression in INDEXED.items():
        schema_editor.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON bookings_customer "
            f"USING gin (({expression}) gin_trgm_ops)"
        )


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for name in INDEXED:
        schema_editor.execute(f"DROP INDEX IF EXISTS {name}")


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0006_customer_phone_search"),
    ]

    operations = [
        migrations.RunPython(create_indexes, drop_indexes),
    ]
