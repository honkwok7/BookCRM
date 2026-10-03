"""Signed links to fill in a form without signing in (for guests, sent by email).

The link carries the assignment id, signed with the site's secret and dated; it works for
``FORM_LINK_DAYS`` days and only while the form is still waiting (a completed or cancelled
form's link shows a "no longer open" page). Nothing secret is stored in the database.
"""

from __future__ import annotations

from django.conf import settings
from django.core import signing
from django.urls import reverse

SALT = "customer_forms.link"


def max_age_seconds() -> int:
    return getattr(settings, "FORM_LINK_DAYS", 30) * 24 * 3600


def make_token(assignment) -> str:
    return signing.dumps(str(assignment.pk), salt=SALT, compress=False)


def link_for(assignment) -> str:
    return settings.SITE_URL + reverse("form-fill", args=[make_token(assignment)])


def read_token(token: str) -> tuple[str | None, bool]:
    """(assignment id, expired). A forged or garbled token gives (None, False)."""
    try:
        return signing.loads(token, salt=SALT, max_age=max_age_seconds()), False
    except signing.SignatureExpired:
        return None, True
    except signing.BadSignature:
        return None, False
