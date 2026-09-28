from __future__ import annotations

from typing import Any

from core.middleware import get_current_request
from core.models import AuditLog


def write_audit_log(
    *,
    action: str,
    object_type: str = "",
    object_identifier: str = "",
    metadata: dict[str, Any] | None = None,
    organization=None,
    user=None,
) -> AuditLog:
    request = get_current_request()
    if request is not None:
        user = user or (request.user if request.user.is_authenticated else None)
        ip = request.META.get("REMOTE_ADDR")
        ua = request.META.get("HTTP_USER_AGENT", "")
    else:
        ip = None
        ua = ""

    return AuditLog.objects.create(
        organization=organization,
        user=user,
        action=action,
        object_type=object_type,
        object_identifier=object_identifier,
        metadata=metadata or {},
        ip_address=ip,
        user_agent=ua,
    )
