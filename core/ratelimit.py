"""Fixed-window rate limits for server-rendered forms (sign-in, password reset).

Counters live in the Django cache. The default cache is per process, so configure the shared
Redis cache (``CACHE_URL``) in production, where several workers serve requests.
"""

from __future__ import annotations

import hashlib

from django.core.cache import cache


def parse_rate(rate: str) -> tuple[int, int]:
    """``"10/900"`` means at most 10 attempts per 900 seconds."""
    count, seconds = rate.split("/")
    return int(count), int(seconds)


def hit(scope: str, identifier: str, rate: str) -> bool:
    """Count one attempt; return True when ``identifier`` is over the limit for ``scope``."""
    limit, window = parse_rate(rate)
    digest = hashlib.sha256(identifier.strip().lower().encode()).hexdigest()
    key = f"ratelimit:{scope}:{digest}"
    cache.add(key, 0, timeout=window)
    try:
        count = cache.incr(key)
    except ValueError:  # expired between add() and incr()
        cache.set(key, 1, timeout=window)
        count = 1
    return count > limit
