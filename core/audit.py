"""Audit logging: the single way to record important actions.

    record_audit(AuditAction.CUSTOMER_UPDATED, organization=org, actor=user,
                 target=customer, changes=diff_instances(before, after))

Guarantees:
- ``action`` comes from the ``AuditAction`` catalogue, so actions are searchable.
- Metadata passes through ``scrub()``. Keys that look secret (password, token, card, …) are
  redacted at any depth. Never put secrets in metadata on purpose; the scrubber is a safety net.
- Personal data in change diffs is recorded as "changed" without values when the model lists
  the field in ``AUDIT_REDACT_FIELDS``. Audit history then survives customer anonymization
  without keeping the old PII.
- The client IP comes from ``REMOTE_ADDR`` unless ``TRUSTED_PROXY_COUNT`` says how many
  reverse proxies append to ``X-Forwarded-For``. Client-supplied headers are never trusted
  blindly.
- Rows are append-only (``AuditLog.save``/``delete`` refuse changes).
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from enum import StrEnum
from typing import Any

from django.conf import settings
from django.db import models

from core.middleware import get_current_request
from core.models import AuditLog

REDACTED = "[REDACTED]"
SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "session",
    "card",
    "cvv",
    "cvc",
    "iban",
    "ssn",
    "sin_number",
    "private_key",
)


class AuditAction(StrEnum):
    # Accounts
    ACCOUNT_EMAIL_VERIFIED = "account.email_verified"
    ACCOUNT_PASSWORD_RESET = "account.password_reset"
    # Organization and membership
    ORGANIZATION_UPDATED = "organization.updated"
    ORGANIZATION_SUSPENDED = "organization.suspended"
    ORGANIZATION_REACTIVATED = "organization.reactivated"
    INVITATION_CREATED = "organization.invitation.created"
    INVITATION_ACCEPTED = "invitation.accepted"
    # Catalogue and team
    SERVICE_CREATED = "service.created"
    SERVICE_UPDATED = "service.updated"
    SERVICE_DELETED = "service.deleted"
    SERVICE_CATEGORY_CREATED = "service_category.created"
    SERVICE_CATEGORY_UPDATED = "service_category.updated"
    SERVICE_CATEGORY_DELETED = "service_category.deleted"
    STAFF_CREATED = "staff.created"
    STAFF_UPDATED = "staff.updated"
    STAFF_DELETED = "staff.deleted"
    LOCATION_CREATED = "location.created"
    LOCATION_UPDATED = "location.updated"
    LOCATION_DELETED = "location.deleted"
    LOCATION_HOURS_UPDATED = "location.hours_updated"
    LOCATION_CLOSURE_CREATED = "location_closure.created"
    LOCATION_CLOSURE_UPDATED = "location_closure.updated"
    LOCATION_CLOSURE_DELETED = "location_closure.deleted"
    # Scheduling
    AVAILABILITY_CREATED = "availability.created"
    AVAILABILITY_UPDATED = "availability.updated"
    AVAILABILITY_DELETED = "availability.deleted"
    AVAILABILITY_EXCEPTION_CREATED = "availability_exception.created"
    AVAILABILITY_EXCEPTION_UPDATED = "availability_exception.updated"
    AVAILABILITY_EXCEPTION_DELETED = "availability_exception.deleted"
    TIME_OFF_CREATED = "time_off.created"
    TIME_OFF_UPDATED = "time_off.updated"
    TIME_OFF_DELETED = "time_off.deleted"
    HOLIDAY_CREATED = "holiday.created"
    HOLIDAY_UPDATED = "holiday.updated"
    HOLIDAY_DELETED = "holiday.deleted"
    # CRM and appointments
    CUSTOMER_CREATED = "customer.created"
    CUSTOMER_UPDATED = "customer.updated"
    CUSTOMER_DELETED = "customer.deleted"
    CUSTOMER_CONSENT_CHANGED = "customer.consent_changed"
    CUSTOMER_MERGED = "customer.merged"
    CUSTOMER_ANONYMIZED = "customer.anonymized"
    CUSTOMER_TAG_ADDED = "customer.tag_added"
    CUSTOMER_TAG_REMOVED = "customer.tag_removed"
    TAG_CREATED = "tag.created"
    NOTE_CREATED = "customer_note.created"
    NOTE_UPDATED = "customer_note.updated"
    NOTE_DELETED = "customer_note.deleted"
    TAG_UPDATED = "tag.updated"
    TAG_DELETED = "tag.deleted"
    WAITLIST_ENTRY_CREATED = "waitlist_entry.created"
    WAITLIST_ENTRY_UPDATED = "waitlist_entry.updated"
    WAITLIST_ENTRY_DELETED = "waitlist_entry.deleted"
    BOOKING_CREATED = "booking.created"
    BOOKING_CANCELLED = "booking.cancelled"
    BOOKING_STATUS_CHANGED = "booking.status_changed"
    BOOKING_RESCHEDULED = "booking.rescheduled"
    # Audit test hook
    SYSTEM_TEST = "system.test"


def _is_sensitive_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def scrub(value: Any) -> Any:
    """Return a JSON-safe copy of ``value`` with sensitive keys redacted at any depth."""
    if isinstance(value, dict):
        return {str(k): (REDACTED if _is_sensitive_key(k) else scrub(v)) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [scrub(v) for v in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, decimal.Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, models.Model):
        return str(value.pk)
    return str(value)


def snapshot(instance: models.Model) -> dict[str, Any]:
    """Concrete field values of ``instance`` (foreign keys as ids), for diffing."""
    data = {}
    for field in instance._meta.concrete_fields:
        if field.name in {"created_at", "updated_at"}:
            continue
        data[field.attname] = getattr(instance, field.attname)
    for field in instance._meta.many_to_many:
        if instance.pk is not None:
            data[field.name] = sorted(
                str(pk) for pk in getattr(instance, field.name).values_list("pk", flat=True)
            )
    return data


def diff_snapshots(before: dict, after: dict, *, redact_fields=()) -> dict[str, Any]:
    """``{field: [old, new]}`` for changed fields; redacted fields become ``"changed"``."""
    changes = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        name = key[:-3] if key.endswith("_id") and key[:-3] in redact_fields else key
        if name in redact_fields or _is_sensitive_key(name):
            changes[name] = "changed"
        else:
            changes[key] = [scrub(old), scrub(new)]
    return changes


def client_ip(request) -> str | None:
    if request is None:
        return None
    proxies = int(getattr(settings, "TRUSTED_PROXY_COUNT", 0) or 0)
    if proxies > 0:
        forwarded = [
            part.strip()
            for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
            if part.strip()
        ]
        if len(forwarded) >= proxies:
            return forwarded[-proxies]
    return request.META.get("REMOTE_ADDR") or None


def record_audit(
    action: AuditAction | str,
    *,
    organization=None,
    actor=None,
    target: models.Model | None = None,
    object_type: str = "",
    object_identifier: str = "",
    metadata: dict | None = None,
    changes: dict | None = None,
    actor_type: str | None = None,
    request=None,
) -> AuditLog:
    action = AuditAction(action)
    request = request if request is not None else get_current_request()
    request_user = getattr(request, "user", None) if request is not None else None
    if actor is None and request_user is not None and request_user.is_authenticated:
        actor = request_user

    if actor_type is None:
        actor_type = AuditLog.ActorType.USER if actor is not None else AuditLog.ActorType.SYSTEM
    if target is not None:
        object_type = object_type or type(target).__name__
        object_identifier = object_identifier or str(target.pk)

    payload = scrub(dict(metadata or {}))
    if changes:
        payload["changes"] = changes

    return AuditLog.objects.create(
        organization=organization,
        user=actor,
        actor_type=actor_type,
        action=action.value,
        object_type=object_type,
        object_identifier=object_identifier,
        metadata=payload,
        ip_address=client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT", "") if request is not None else "")[:1000],
    )
