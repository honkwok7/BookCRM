"""Writes to a provider's time off (M5.3). Views call these; they don't save TimeOff rows.

- ``request_time_off``: the provider asks; pending until approved, so it blocks nothing yet.
- ``block_time``: the provider blocks part of one day (a break, admin time); approved at once,
  but refused when an appointment is on then.
- ``decide_time_off``: someone with ``staff.manage`` approves or rejects a pending request.
- ``cancel_time_off``: the provider (or a manager) withdraws a request, or time off that has
  not started yet.

Anything that makes time off approved locks the provider's calendar row first (the same lock
``bookings.services`` takes to create or move an appointment), so a block and a booking for the
same time cannot both succeed.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from bookings.models import Booking
from core.audit import AuditAction, record_audit
from scheduling.availability import ACTIVE_STATUSES
from scheduling.models import TimeOff
from staff.models import StaffProfile

Status = TimeOff.ApprovalStatus
MAX_BLOCK = timedelta(hours=12)
MAX_BLOCKED_PER_WEEK = timedelta(hours=12)  # all blocks in a week (Monday to Sunday)
MAX_REQUEST = timedelta(days=90)
REASON_LENGTH = TimeOff._meta.get_field("reason").max_length


def _lock_staff(staff: StaffProfile) -> StaffProfile:
    return StaffProfile.objects.select_for_update().get(pk=staff.pk)


def _check_period(start: datetime, end: datetime, *, now: datetime, longest: timedelta):
    """Checks in UTC: two datetimes with the same tzinfo compare by wall clock in Python, which
    is wrong across a clock change. Returns the period in UTC."""
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start >= end:
        raise ValidationError("The end must be after the start.")
    if end <= now:
        raise ValidationError("That time has already passed.")
    if end - start > longest:
        raise ValidationError(f"Choose at most {_describe(longest)}.")
    return start, end


def _describe(span: timedelta) -> str:
    if span.days:
        return f"{span.days} days"
    return f"{span.seconds // 3600} hours"


def appointments_during(staff: StaffProfile, start: datetime, end: datetime):
    """The provider's appointments still on that overlap ``[start, end)``."""
    return Booking.objects.filter(
        staff=staff,
        status__in=ACTIVE_STATUSES,
        start_datetime__lt=end,
        end_datetime__gt=start,
    ).order_by("start_datetime")


def _audit(action, entry: TimeOff, actor, **metadata):
    record_audit(
        action,
        organization=entry.organization,
        actor=actor,
        target=entry,
        metadata={
            "staff": str(entry.staff_id),
            "start": entry.start_datetime.isoformat(),
            "end": entry.end_datetime.isoformat(),
            **metadata,
        },
    )


def request_time_off(
    *, staff: StaffProfile, start: datetime, end: datetime, reason: str = "", actor=None
) -> TimeOff:
    now = timezone.now()
    start, end = _check_period(start, end, now=now, longest=MAX_REQUEST)
    with transaction.atomic():
        entry = TimeOff.objects.create(
            organization=staff.organization,
            staff=staff,
            start_datetime=start,
            end_datetime=end,
            reason=reason[:REASON_LENGTH],
            approval_status=Status.PENDING,
        )
        _audit(AuditAction.TIME_OFF_REQUESTED, entry, actor)
    return entry


def _weeks(start: datetime, end: datetime, zone) -> list[tuple[datetime, datetime]]:
    """The weeks (Monday 00:00 to Monday 00:00, wall clock in ``zone``) ``[start, end)``
    touches, in UTC."""
    weeks = []
    day = start.astimezone(zone).date()
    monday = day - timedelta(days=day.weekday())
    while True:
        week_start = datetime.combine(monday, time.min, tzinfo=zone).astimezone(UTC)
        week_end = datetime.combine(monday + timedelta(days=7), time.min, tzinfo=zone)
        week_end = week_end.astimezone(UTC)
        weeks.append((week_start, week_end))
        if end <= week_end:
            return weeks
        monday += timedelta(days=7)


def _check_weekly_blocks(staff: StaffProfile, start: datetime, end: datetime) -> None:
    """Blocks add up to at most ``MAX_BLOCKED_PER_WEEK`` in any week, so short blocks can't be
    chained into an absence nobody approved. More is a time-off request."""
    try:
        zone = ZoneInfo(staff.organization.timezone)
    except ZoneInfoNotFoundError, ValueError:
        zone = UTC
    for week_start, week_end in _weeks(start, end, zone):
        blocks = TimeOff.objects.filter(
            staff=staff,
            self_blocked=True,
            approval_status=Status.APPROVED,
            start_datetime__lt=week_end,
            end_datetime__gt=week_start,
        )
        periods = [(entry.start_datetime, entry.end_datetime) for entry in blocks]
        periods.append((start, end))
        blocked = sum(
            (min(e, week_end) - max(s, week_start) for s, e in periods if s < week_end),
            timedelta(),
        )
        if blocked > MAX_BLOCKED_PER_WEEK:
            hours = int(MAX_BLOCKED_PER_WEEK.total_seconds() // 3600)
            raise ValidationError(
                f"Blocks can add up to {hours} hours a week, and this would go over. "
                "Request time off instead, so it can be approved."
            )


def block_time(
    *, staff: StaffProfile, start: datetime, end: datetime, reason: str = "", actor=None
) -> TimeOff:
    now = timezone.now()
    start, end = _check_period(start, end, now=now, longest=MAX_BLOCK)
    with transaction.atomic():
        _lock_staff(staff)
        clashes = appointments_during(staff, start, end).count()
        if clashes:
            raise ValidationError(
                f"You have {clashes} appointment{'s' if clashes != 1 else ''} then. "
                "Move or cancel them first, or request time off instead."
            )
        _check_weekly_blocks(staff, start, end)
        entry = TimeOff.objects.create(
            organization=staff.organization,
            staff=staff,
            start_datetime=start,
            end_datetime=end,
            reason=(reason or "Blocked")[:REASON_LENGTH],
            approval_status=Status.APPROVED,
            self_blocked=True,
        )
        _audit(AuditAction.TIME_BLOCKED, entry, actor)
    return entry


def decide_time_off(*, entry: TimeOff, approve: bool, actor=None) -> TimeOff:
    """Approve or reject a pending request. Approving does not move appointments already
    booked then; the page shows how many there are before the manager decides."""
    with transaction.atomic():
        _lock_staff(entry.staff)
        entry = TimeOff.objects.select_for_update().get(pk=entry.pk)
        if entry.approval_status != Status.PENDING:
            raise ValidationError("This request has already been decided.")
        entry.approval_status = Status.APPROVED if approve else Status.REJECTED
        entry.save(update_fields=["approval_status", "updated_at"])
        clashes = appointments_during(entry.staff, entry.start_datetime, entry.end_datetime)
        _audit(
            AuditAction.TIME_OFF_APPROVED if approve else AuditAction.TIME_OFF_REJECTED,
            entry,
            actor,
            appointments_then=clashes.count() if approve else 0,
        )
    return entry


def can_cancel(entry: TimeOff, *, now: datetime | None = None) -> bool:
    now = now or timezone.now()
    if entry.approval_status == Status.PENDING:
        return entry.end_datetime > now
    return entry.approval_status == Status.APPROVED and entry.start_datetime > now


def cancel_time_off(*, entry: TimeOff, actor=None) -> TimeOff:
    with transaction.atomic():
        entry = TimeOff.objects.select_for_update().get(pk=entry.pk)
        if not can_cancel(entry):
            raise ValidationError(
                "Only requests, or time off that hasn't started, can be cancelled."
            )
        entry.approval_status = Status.CANCELLED
        entry.save(update_fields=["approval_status", "updated_at"])
        _audit(AuditAction.TIME_OFF_CANCELLED, entry, actor)
    return entry
