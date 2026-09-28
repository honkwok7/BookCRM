"""The customer timeline: ``record_activity`` is the only writer of ``CustomerActivity``.

Called from the booking, notification and CRM services inside their transactions, so an
entry exists exactly when the change it describes was committed.

Metadata holds ids and codes only (references, statuses, tag ids), never names, emails or
note text: the timeline then needs no rewriting when a customer is anonymized.
"""

from __future__ import annotations

from django.db import models

from core.audit import scrub
from crm.models import CustomerActivity

Kind = CustomerActivity.Kind


def record_activity(
    kind: Kind | str,
    *,
    customer,
    actor=None,
    subject: models.Model | None = None,
    metadata: dict | None = None,
    internal: bool = False,
    occurred_at=None,
) -> CustomerActivity | None:
    """Add a timeline entry. ``customer`` may be None (e.g. a booking without one): no-op."""
    if customer is None:
        return None
    fields = {}
    if occurred_at is not None:
        fields["occurred_at"] = occurred_at
    return CustomerActivity.objects.create(
        organization_id=customer.organization_id,
        customer=customer,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        kind=Kind(kind),
        subject_type=subject._meta.label_lower if subject is not None else "",
        subject_id=str(subject.pk) if subject is not None else "",
        metadata=scrub(dict(metadata or {})),
        internal=internal,
        **fields,
    )


def booking_metadata(booking) -> dict:
    return {
        "reference": booking.reference,
        "service": str(booking.service_id),
        "staff": str(booking.staff_id),
        "start": booking.start_datetime.isoformat(),
    }
