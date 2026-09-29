"""Location writes: the one place that creates, changes or removes locations, their opening
hours and closures.

The API, the web app, ``seed_demo`` and future AI agents call these functions, so the plan
limit, the single default location, hour and closure validation, and auditing apply the same
way everywhere. Errors are ``DomainError`` (400) or ``ConflictError`` (409) with a stable
``code``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, time

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import ProtectedError
from django.utils.text import slugify

from core.audit import AuditAction, diff_snapshots, record_audit, snapshot
from core.exceptions import ConflictError, DomainError
from locations.models import Location, LocationClosure, LocationHours, validate_timezone
from organizations.models import Organization

EDITABLE_FIELDS = frozenset(
    {
        "name",
        "address_line1",
        "address_line2",
        "city",
        "region",
        "postal_code",
        "country",
        "timezone",
        "phone",
        "email",
        "is_active",
        "booking_enabled",
    }
)
MAX_PERIODS_PER_DAY = 2
DEFAULT_LOCATION_NAME = "Main"


def _check_fields(fields: dict) -> None:
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise DomainError(f"Unknown location fields: {', '.join(sorted(unknown))}", code="invalid")


def _clean(location: Location) -> None:
    location.name = (location.name or "").strip()
    if not location.name:
        raise DomainError("A location name is required", code="name_required")
    location.country = (location.country or "").strip().upper()
    try:
        validate_timezone(location.timezone)
    except ValidationError as error:
        raise DomainError(error.messages[0], code="invalid_timezone") from error


def _unique_slug(organization, name: str) -> str:
    base = slugify(name)[:120] or "location"
    taken = set(
        Location.objects.filter(organization=organization, slug__startswith=base).values_list(
            "slug", flat=True
        )
    )
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _lock_organization(organization) -> Organization:
    """Serialize location writes per organization (plan limit, single default)."""
    return Organization.objects.select_for_update().get(pk=organization.pk)


def _check_plan_limit(organization) -> None:
    from subscriptions.services import enforce_plan_limit

    try:
        enforce_plan_limit(organization, "locations")
    except ValueError as error:
        raise ConflictError(str(error), code="plan_limit") from error


def _audit(action, location, actor, **kwargs):
    record_audit(action, organization=location.organization, actor=actor, target=location, **kwargs)


def default_location_fields(organization) -> dict:
    """Fields for an organization's first location, taken from its profile."""
    lines = [line.strip() for line in (organization.address or "").splitlines() if line.strip()]
    return {
        "name": DEFAULT_LOCATION_NAME,
        "address_line1": lines[0][:255] if lines else "",
        "address_line2": ", ".join(lines[1:])[:255],
        "timezone": organization.timezone or "UTC",
        "phone": organization.phone or "",
        "email": organization.email or "",
    }


@transaction.atomic
def ensure_default_location(organization) -> Location:
    """The organization's default location, creating "Main" from its profile if needed.

    Idempotent. Runs when an organization is created (``locations.signals``); an organization
    that somehow lost its default gets its first active location promoted instead.
    """
    organization = _lock_organization(organization)
    default = Location.objects.filter(organization=organization, is_default=True).first()
    if default is not None:
        return default
    promoted = Location.objects.filter(organization=organization, is_active=True).first()
    if promoted is not None:
        promoted.is_default = True
        promoted.save(update_fields=["is_default", "updated_at"])
        return promoted
    fields = default_location_fields(organization)
    try:
        validate_timezone(fields["timezone"])
    except ValidationError:
        fields["timezone"] = "UTC"
    return Location.objects.create(
        organization=organization,
        slug=_unique_slug(organization, fields["name"]),
        is_default=True,
        **fields,
    )


@transaction.atomic
def create_location(*, organization, actor=None, **fields) -> Location:
    _check_fields(fields)
    organization = _lock_organization(organization)
    fields.setdefault("timezone", organization.timezone or "UTC")
    location = Location(organization=organization, **fields)
    _clean(location)
    if location.is_active:
        _check_plan_limit(organization)
    location.slug = _unique_slug(organization, location.name)
    location.is_default = not Location.objects.filter(
        organization=organization, is_default=True
    ).exists()
    if location.is_default and not location.is_active:
        raise DomainError("The default location must be active", code="default_location")
    location.save()
    _audit(AuditAction.LOCATION_CREATED, location, actor, metadata={"name": location.name})
    return location


@transaction.atomic
def update_location(*, location: Location, actor=None, **changes) -> Location:
    _check_fields(changes)
    _lock_organization(location.organization)
    location = Location.objects.select_related("organization").get(pk=location.pk)
    before = snapshot(location)
    was_active = location.is_active
    for name, value in changes.items():
        setattr(location, name, value)
    _clean(location)
    if location.is_default and not location.is_active:
        raise ConflictError(
            "The default location can't be deactivated. Make another location the default "
            "first.",
            code="default_location",
        )
    if location.is_active and not was_active:
        _check_plan_limit(location.organization)
    location.save()
    diff = diff_snapshots(before, snapshot(location))
    if diff:
        _audit(AuditAction.LOCATION_UPDATED, location, actor, changes=diff)
    return location


@transaction.atomic
def set_default_location(*, location: Location, actor=None) -> Location:
    _lock_organization(location.organization)
    location = Location.objects.select_related("organization").get(pk=location.pk)
    if location.is_default:
        return location
    if not location.is_active:
        raise ConflictError("Only an active location can be the default", code="inactive")
    previous = Location.objects.filter(
        organization_id=location.organization_id, is_default=True
    ).first()
    if previous is not None:
        # Clear the old default first: at most one default per organization is a constraint.
        previous.is_default = False
        previous.save(update_fields=["is_default", "updated_at"])
    location.is_default = True
    location.save(update_fields=["is_default", "updated_at"])
    _audit(
        AuditAction.LOCATION_UPDATED,
        location,
        actor,
        changes={"is_default": [False, True]},
        metadata={"previous_default": str(previous.pk) if previous else None},
    )
    return location


@transaction.atomic
def delete_location(*, location: Location, actor=None) -> None:
    _lock_organization(location.organization)
    location = Location.objects.select_related("organization").get(pk=location.pk)
    if location.is_default:
        raise ConflictError(
            "The default location can't be deleted. Make another location the default first.",
            code="default_location",
        )
    _audit(AuditAction.LOCATION_DELETED, location, actor, metadata={"name": location.name})
    try:
        with transaction.atomic():
            location.delete()
    except ProtectedError as error:
        raise ConflictError(
            "This location is in use. Deactivate it instead.", code="in_use"
        ) from error


# -- Opening hours --------------------------------------------------------------------------


def _check_hours(periods) -> list[tuple[int, time, time]]:
    by_day: dict[int, list[tuple[time, time]]] = defaultdict(list)
    for weekday, opens_at, closes_at in periods:
        if weekday not in range(7):
            raise DomainError("Weekday must be 0 (Monday) to 6 (Sunday)", code="invalid_weekday")
        if opens_at >= closes_at:
            raise DomainError("Opening time must be before closing time", code="invalid_hours")
        by_day[weekday].append((opens_at, closes_at))
    cleaned = []
    for weekday, day_periods in sorted(by_day.items()):
        day_periods.sort()
        if len(day_periods) > MAX_PERIODS_PER_DAY:
            raise DomainError(
                f"At most {MAX_PERIODS_PER_DAY} opening periods a day", code="too_many_periods"
            )
        for (_, first_close), (second_open, _) in zip(day_periods, day_periods[1:], strict=False):
            if second_open < first_close:
                raise DomainError("Opening periods on a day can't overlap", code="overlapping")
        cleaned += [(weekday, opens_at, closes_at) for opens_at, closes_at in day_periods]
    return cleaned


def _hours_summary(location) -> list[str]:
    return [str(period) for period in location.hours.order_by("weekday", "opens_at")]


@transaction.atomic
def set_location_hours(*, location: Location, periods, actor=None) -> list[LocationHours]:
    """Replace the weekly opening hours with ``periods``: ``(weekday, opens_at, closes_at)``.

    An empty list clears the hours (the location then doesn't restrict bookings).
    """
    periods = _check_hours(periods)
    location = (
        Location.objects.select_for_update().select_related("organization").get(pk=location.pk)
    )
    before = _hours_summary(location)
    location.hours.all().delete()
    rows = LocationHours.objects.bulk_create(
        LocationHours(
            organization_id=location.organization_id,
            location=location,
            weekday=weekday,
            opens_at=opens_at,
            closes_at=closes_at,
        )
        for weekday, opens_at, closes_at in periods
    )
    after = _hours_summary(location)
    if before != after:
        _audit(
            AuditAction.LOCATION_HOURS_UPDATED,
            location,
            actor,
            changes={"hours": [before, after]},
        )
    return rows


# -- Closures -------------------------------------------------------------------------------


def _clean_closure(closure: LocationClosure) -> None:
    if closure.end_date is None:
        closure.end_date = closure.start_date
    if not isinstance(closure.start_date, date) or closure.end_date < closure.start_date:
        raise DomainError("The last day can't be before the first day", code="invalid_dates")
    if closure.all_day:
        closure.start_time = closure.end_time = None
    elif closure.start_time is None or closure.end_time is None:
        raise DomainError("Enter the times the location is closed", code="times_required")
    elif closure.start_time >= closure.end_time:
        raise DomainError(
            "The closing time must be before the reopening time", code="invalid_times"
        )
    closure.reason = (closure.reason or "").strip()


def _closure_metadata(closure: LocationClosure) -> dict:
    return {
        "location": str(closure.location_id),
        "start_date": closure.start_date,
        "end_date": closure.end_date,
        "all_day": closure.all_day,
    }


@transaction.atomic
def create_closure(
    *,
    location: Location,
    start_date: date,
    end_date: date | None = None,
    all_day: bool = True,
    start_time: time | None = None,
    end_time: time | None = None,
    reason: str = "",
    actor=None,
) -> LocationClosure:
    closure = LocationClosure(
        organization_id=location.organization_id,
        location=location,
        start_date=start_date,
        end_date=end_date,
        all_day=all_day,
        start_time=start_time,
        end_time=end_time,
        reason=reason,
    )
    _clean_closure(closure)
    closure.save()
    record_audit(
        AuditAction.LOCATION_CLOSURE_CREATED,
        organization=location.organization,
        actor=actor,
        target=closure,
        metadata=_closure_metadata(closure),
    )
    return closure


CLOSURE_FIELDS = frozenset(
    {"start_date", "end_date", "all_day", "start_time", "end_time", "reason"}
)


@transaction.atomic
def update_closure(*, closure: LocationClosure, actor=None, **changes) -> LocationClosure:
    unknown = set(changes) - CLOSURE_FIELDS
    if unknown:
        raise DomainError(f"Unknown closure fields: {', '.join(sorted(unknown))}", code="invalid")
    closure = LocationClosure.objects.select_for_update().get(pk=closure.pk)
    before = snapshot(closure)
    for name, value in changes.items():
        setattr(closure, name, value)
    _clean_closure(closure)
    closure.save()
    diff = diff_snapshots(before, snapshot(closure))
    if diff:
        record_audit(
            AuditAction.LOCATION_CLOSURE_UPDATED,
            organization=closure.location.organization,
            actor=actor,
            target=closure,
            changes=diff,
        )
    return closure


@transaction.atomic
def delete_closure(*, closure: LocationClosure, actor=None) -> None:
    record_audit(
        AuditAction.LOCATION_CLOSURE_DELETED,
        organization=closure.location.organization,
        actor=actor,
        target=closure,
        metadata=_closure_metadata(closure),
    )
    closure.delete()
