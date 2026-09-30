"""Feature flags (M5.5b): ``is_enabled("new-calendar", organization)``.

An organization's override wins over the flag's global setting; an unknown flag is off. The
answer is cached for a minute (the platform pages clear it on every change).
"""

from __future__ import annotations

from django.core.cache import cache

from saas.models import FeatureFlag, FeatureFlagOverride

CACHE_SECONDS = 60


def _key(flag_key: str) -> str:
    return f"saas:flag:{flag_key}"


def is_enabled(flag_key: str, organization=None) -> bool:
    state = cache.get(_key(flag_key))
    if state is None:
        flag = FeatureFlag.objects.filter(key=flag_key).first()
        state = {
            "enabled": bool(flag and flag.enabled),
            "overrides": (
                {
                    str(org_id): enabled
                    for org_id, enabled in FeatureFlagOverride.objects.filter(
                        flag=flag
                    ).values_list("organization_id", "enabled")
                }
                if flag
                else {}
            ),
        }
        cache.set(_key(flag_key), state, CACHE_SECONDS)
    if organization is not None and str(organization.pk) in state["overrides"]:
        return state["overrides"][str(organization.pk)]
    return state["enabled"]


def forget(flag_key: str) -> None:
    cache.delete(_key(flag_key))
