"""Give every organization without a default location one (``manage.py
ensure_default_locations``).

Organizations get their default location when created (``locations.signals``), except through
``loaddata``: fixtures are raw saves, which skip signals. Run this after loading fixtures that
contain organizations without their locations. Idempotent.
"""

from django.core.management.base import BaseCommand

from locations.models import Location
from locations.services import ensure_default_location
from organizations.models import Organization


class Command(BaseCommand):
    help = "Create (or promote) a default location for every organization that has none."

    def handle(self, *args, **options):
        with_default = Location.objects.filter(is_default=True).values("organization_id")
        missing = Organization.objects.exclude(pk__in=with_default)
        count = 0
        for organization in missing:
            ensure_default_location(organization)
            count += 1
        self.stdout.write(f"Default locations created or promoted: {count}")
