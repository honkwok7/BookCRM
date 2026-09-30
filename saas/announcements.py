"""Which platform announcements a signed-in person sees (M5.5b).

The banner loads separately (htmx, ``/announcements/``) so pages keep their query budgets. The
running announcements are cached for a minute; dismissing one hides it for the rest of the
browser session.
"""

from __future__ import annotations

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from organizations.models import OrganizationMembership, OrganizationRole
from saas.models import Announcement

CACHE_KEY = "saas:announcements"
CACHE_SECONDS = 60
DISMISSED = "dismissed_announcements"
Audience = Announcement.Audience


def running() -> list[Announcement]:
    rows = cache.get(CACHE_KEY)
    if rows is None:
        now = timezone.now()
        rows = list(
            Announcement.objects.filter(is_active=True, starts_at__lte=now).filter(
                Q(ends_at__isnull=True) | Q(ends_at__gt=now)
            )
        )
        cache.set(CACHE_KEY, rows, CACHE_SECONDS)
    return [row for row in rows if row.is_running()]


def forget_running() -> None:
    cache.delete(CACHE_KEY)


def for_request(request, where: str) -> list[Announcement]:
    """``where``: "app" (the organization app) or "portal"."""
    user = request.user
    if not user.is_authenticated:
        return []
    rows = running()
    if not rows:
        return []
    dismissed = set(request.session.get(DISMISSED, []))
    audiences = {Audience.EVERYONE}
    if where == "portal":
        audiences.add(Audience.CUSTOMERS)
    else:
        roles = set(
            OrganizationMembership.objects.filter(user=user, is_active=True).values_list(
                "role", flat=True
            )
        )
        if roles - {OrganizationRole.CUSTOMER}:
            audiences.add(Audience.TEAMS)
        if OrganizationRole.OWNER in roles:
            audiences.add(Audience.OWNERS)
    return [row for row in rows if row.audience in audiences and str(row.pk) not in dismissed]
