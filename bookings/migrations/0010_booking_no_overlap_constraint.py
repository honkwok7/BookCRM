"""M4.2: PostgreSQL guarantees that one staff member's active appointments never overlap.

An exclusion constraint on (staff, [start, end)) for the active statuses, so no code path (a
bug, a raw query, a future import) can double-book, even without the booking service's lock.
Buffers stay an application rule (the availability engine), because the gap needed between two
appointments is the larger of their buffers, which a per-row range can't express.

Other databases skip it (development only). If existing data already overlaps, the migration
stops and lists the appointments to fix first: ``manage.py check_booking_overlaps``.
btree_gist is a trusted extension (PostgreSQL 13+): the database owner can create it.
"""

from django.db import migrations

NAME = "booking_staff_no_overlap"
ACTIVE = ("pending", "confirmed", "checked_in", "in_progress")
ACTIVE_SQL = ", ".join(f"'{status}'" for status in ACTIVE)

OVERLAPS = f"""
    SELECT a.reference, b.reference
    FROM bookings_booking a
    JOIN bookings_booking b
      ON a.staff_id = b.staff_id AND a.id < b.id
     AND a.start_datetime < b.end_datetime AND b.start_datetime < a.end_datetime
    WHERE a.status IN ({ACTIVE_SQL}) AND b.status IN ({ACTIVE_SQL})
    LIMIT 50
"""


def add_constraint(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(OVERLAPS)
        offenders = cursor.fetchall()
    if offenders:
        pairs = ", ".join(f"{first}/{second}" for first, second in offenders)
        raise RuntimeError(
            f"Overlapping active appointments must be fixed before this migration: {pairs}. "
            "Run manage.py check_booking_overlaps for the full list."
        )
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    schema_editor.execute(
        f"ALTER TABLE bookings_booking ADD CONSTRAINT {NAME} EXCLUDE USING gist "
        "(staff_id WITH =, tstzrange(start_datetime, end_datetime, '[)') WITH &&) "
        f"WHERE (status IN ({ACTIVE_SQL}))"
    )


def drop_constraint(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f"ALTER TABLE bookings_booking DROP CONSTRAINT IF EXISTS {NAME}")


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0009_backfill_booking_location_and_buffers"),
    ]

    operations = [migrations.RunPython(add_constraint, drop_constraint)]
