"""Platform background tasks (M5.5)."""

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

HEARTBEAT_KEY = "platform:celery-heartbeat"


@shared_task
def heartbeat() -> str:
    """Run by celery beat every minute; the platform dashboard warns when it stops."""
    now = timezone.now().isoformat()
    cache.set(HEARTBEAT_KEY, now, 60 * 60)
    return now
