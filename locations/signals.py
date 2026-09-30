"""Every organization gets its default location the moment it is created, whichever code path
creates it (admin, seed_demo, a future sign-up flow). Existing organizations got theirs from
migration 0002. ``loaddata`` (raw saves) skips signals: run ``manage.py
ensure_default_locations`` after loading organization fixtures."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from organizations.models import Organization


@receiver(post_save, sender=Organization, dispatch_uid="locations_default_location")
def create_default_location(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        from locations.services import ensure_default_location

        ensure_default_location(instance)
