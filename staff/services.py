"""Staff writes: profiles, the locations people work at, and the services they offer where.

The API, the web app, ``seed_demo`` and future AI agents call these functions, so membership,
the plan's staff limit, same-organization checks and auditing apply the same way everywhere.
Errors are ``DomainError`` (400) or ``ConflictError`` (409) with a stable ``code``.

"Who offers what, where" lives in ``StaffServiceOffering``: an offering without a location
means "at every location this person works at". ``Service.assigned_staff_members`` is kept as
a mirror (staff with an active offering) for older readers; only this module writes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, Q

from core.audit import AuditAction, diff_snapshots, record_audit, snapshot
from core.exceptions import ConflictError, DomainError
from locations.models import Location
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from services.models import Service
from staff.models import StaffProfile, StaffServiceOffering

PROFILE_FIELDS = frozenset(
    {
        "display_name",
        "job_title",
        "provider_type",
        "bio",
        "phone_number",
        "is_active",
        "is_accepting_bookings",
        "online_booking_visible",
        "max_daily_appointments",
        "appointment_color",
    }
)
# Recorded in audit diffs as "changed" without values.
REDACTED_FIELDS = ("phone_number",)
OFFERING_FIELDS = frozenset({"custom_duration_minutes", "custom_price", "is_active"})


def _audit(action, target, actor, organization=None, **kwargs):
    record_audit(
        action,
        organization=organization or target.organization,
        actor=actor,
        target=target,
        **kwargs,
    )


def _check_profile_fields(fields: dict) -> None:
    unknown = set(fields) - PROFILE_FIELDS
    if unknown:
        raise DomainError(f"Unknown staff fields: {', '.join(sorted(unknown))}", code="invalid")
    for name in ("display_name", "job_title", "provider_type"):
        if isinstance(fields.get(name), str):
            fields[name] = fields[name].strip()
    if fields.get("max_daily_appointments") == 0:
        raise DomainError(
            "The daily limit must be at least 1, or empty for no limit", code="invalid_limit"
        )


def _check_plan_limit(organization) -> None:
    from subscriptions.services import enforce_plan_limit

    try:
        enforce_plan_limit(organization, "staff")
    except ValueError as error:
        raise ConflictError(str(error), code="plan_limit") from error


def _check_locations(organization, locations) -> list[Location]:
    locations = list(locations)
    ids = {location.pk for location in locations}
    found = Location.objects.filter(organization=organization, is_active=True, pk__in=ids)
    if found.count() != len(ids):
        raise DomainError("Location not found or inactive", code="invalid_location")
    return locations


def _lock_staff(staff) -> StaffProfile:
    return (
        StaffProfile.objects.select_for_update()
        .select_related("organization", "user")
        .get(pk=staff.pk)
    )


# -- Profiles -------------------------------------------------------------------------------


@transaction.atomic
def create_staff_profile(*, organization, user, actor=None, locations=None, **fields):
    """Make a team member bookable as a provider.

    ``user`` must be an active, non-customer member of the organization. ``locations``
    defaults to the organization's default location.
    """
    _check_profile_fields(fields)
    organization = Organization.objects.select_for_update().get(pk=organization.pk)
    membership = OrganizationMembership.objects.filter(
        organization=organization, user=user, is_active=True
    ).first()
    if membership is None or membership.role == OrganizationRole.CUSTOMER:
        raise DomainError("Choose an active member of your team", code="not_a_member")
    if StaffProfile.objects.filter(organization=organization, user=user).exists():
        raise ConflictError("This person already has a staff profile", code="duplicate")
    if fields.get("is_active", True):
        _check_plan_limit(organization)
    if locations is None:
        locations = Location.objects.filter(organization=organization, is_default=True)
    locations = _check_locations(organization, locations)

    staff = StaffProfile.objects.create(organization=organization, user=user, **fields)
    staff.locations.set(locations)
    _audit(
        AuditAction.STAFF_CREATED,
        staff,
        actor,
        metadata={"user": str(user.pk), "locations": [str(loc.pk) for loc in locations]},
    )
    return staff


@transaction.atomic
def update_staff_profile(*, staff: StaffProfile, actor=None, **changes) -> StaffProfile:
    _check_profile_fields(changes)
    # Lock order for staff writes: organization, then the staff row. The organization lock
    # makes the plan-limit count on reactivation safe against parallel reactivations.
    Organization.objects.select_for_update().get(pk=staff.organization_id)
    staff = _lock_staff(staff)
    before = snapshot(staff)
    was_active = staff.is_active
    for name, value in changes.items():
        setattr(staff, name, value)
    if staff.is_active and not was_active:
        _check_plan_limit(staff.organization)
    staff.save()
    diff = diff_snapshots(before, snapshot(staff), redact_fields=REDACTED_FIELDS)
    if diff:
        _audit(AuditAction.STAFF_UPDATED, staff, actor, changes=diff)
        if "provider_type" in diff:
            sync_service_mirrors(staff.offerings.values_list("service_id", flat=True))
    return staff


@transaction.atomic
def delete_staff_profile(*, staff: StaffProfile, actor=None) -> None:
    staff = _lock_staff(staff)
    service_ids = list(staff.offerings.values_list("service_id", flat=True))
    _audit(AuditAction.STAFF_DELETED, staff, actor, metadata={"user": str(staff.user_id)})
    try:
        with transaction.atomic():
            staff.delete()
    except ProtectedError as error:
        raise ConflictError(
            "This person has appointments. Deactivate them instead.", code="in_use"
        ) from error
    sync_service_mirrors(service_ids)


@transaction.atomic
def set_staff_locations(*, staff: StaffProfile, locations, actor=None) -> StaffProfile:
    """Replace the locations ``staff`` works at.

    Offerings tied to a location they no longer work at are removed with it.
    """
    staff = _lock_staff(staff)
    locations = _check_locations(staff.organization, locations)
    before = sorted(str(pk) for pk in staff.locations.values_list("pk", flat=True))
    after = sorted(str(location.pk) for location in locations)
    if before == after:
        return staff
    dropped = staff.offerings.filter(location__isnull=False).exclude(
        location__in=[location.pk for location in locations]
    )
    service_ids = list(dropped.values_list("service_id", flat=True))
    removed = dropped.count()
    dropped.delete()
    staff.locations.set(locations)
    _audit(
        AuditAction.STAFF_LOCATIONS_CHANGED,
        staff,
        actor,
        changes={"locations": [before, after]},
        metadata={"offerings_removed": removed},
    )
    sync_service_mirrors(service_ids)
    return staff


# -- Offerings ------------------------------------------------------------------------------


def _check_offering(staff: StaffProfile, service: Service, location: Location | None) -> None:
    if service.organization_id != staff.organization_id or service.is_archived:
        raise DomainError("Service not found", code="invalid_service")
    required = service.required_provider_type
    if required and staff.provider_type.strip().lower() != required.lower():
        raise DomainError(
            f"{service.name} can only be offered by a {required}", code="provider_type_mismatch"
        )
    if location is not None:
        if location.organization_id != staff.organization_id:
            raise DomainError("Location not found or inactive", code="invalid_location")
        if not staff.locations.filter(pk=location.pk).exists():
            raise DomainError(
                "This person doesn't work at that location", code="location_not_assigned"
            )
        if service.locations.exists() and not service.locations.filter(pk=location.pk).exists():
            raise DomainError(
                f"{service.name} isn't offered at {location.name}", code="service_not_at_location"
            )


def _check_custom_values(duration, price) -> None:
    if duration is not None and duration <= 0:
        raise DomainError("The duration must be at least one minute", code="invalid_duration")
    if price is not None and Decimal(price) < 0:
        raise DomainError("The price can't be negative", code="invalid_price")


def _offering_metadata(offering) -> dict:
    return {
        "staff": str(offering.staff_id),
        "service": str(offering.service_id),
        "location": str(offering.location_id) if offering.location_id else None,
    }


def valid_offerings(service: Service):
    """The service's active offerings that still fit its rules: the provider has the
    required type (if any), and a location-specific offering is at a location where the
    service is offered. Offerings that stopped fitting are kept (so a fix restores them) but
    never count."""
    offerings = StaffServiceOffering.objects.filter(service=service, is_active=True)
    if service.required_provider_type:
        offerings = offerings.filter(staff__provider_type__iexact=service.required_provider_type)
    service_locations = list(service.locations.values_list("pk", flat=True))
    if service_locations:
        offerings = offerings.filter(Q(location__isnull=True) | Q(location__in=service_locations))
    return offerings


def sync_service_mirrors(service_ids) -> None:
    """Rebuild ``Service.assigned_staff_members`` for these services: staff with a valid
    offering. Called after every change to offerings, a service's rules or a provider type."""
    for service in Service.objects.filter(pk__in=set(service_ids)):
        staff_ids = valid_offerings(service).values_list("staff_id", flat=True).distinct()
        service.assigned_staff_members.set(list(staff_ids))


@transaction.atomic
def add_offering(
    *,
    staff: StaffProfile,
    service: Service,
    location: Location | None = None,
    custom_duration_minutes: int | None = None,
    custom_price=None,
    is_active: bool = True,
    actor=None,
) -> StaffServiceOffering:
    staff = _lock_staff(staff)
    _check_offering(staff, service, location)
    _check_custom_values(custom_duration_minutes, custom_price)
    try:
        with transaction.atomic():
            offering = StaffServiceOffering.objects.create(
                organization_id=staff.organization_id,
                staff=staff,
                service=service,
                location=location,
                custom_duration_minutes=custom_duration_minutes,
                custom_price=custom_price,
                is_active=is_active,
            )
    except IntegrityError as error:
        raise ConflictError(
            "This person already offers this service there", code="duplicate"
        ) from error
    _audit(
        AuditAction.STAFF_SERVICE_ADDED,
        offering,
        actor,
        organization=staff.organization,
        metadata=_offering_metadata(offering),
    )
    sync_service_mirrors([service.pk])
    return offering


@transaction.atomic
def update_offering(*, offering: StaffServiceOffering, actor=None, **changes):
    unknown = set(changes) - OFFERING_FIELDS
    if unknown:
        raise DomainError(f"Unknown offering fields: {', '.join(sorted(unknown))}", code="invalid")
    offering = StaffServiceOffering.objects.select_for_update().get(pk=offering.pk)
    before = snapshot(offering)
    for name, value in changes.items():
        setattr(offering, name, value)
    _check_custom_values(offering.custom_duration_minutes, offering.custom_price)
    offering.save()
    diff = diff_snapshots(before, snapshot(offering))
    if diff:
        _audit(
            AuditAction.STAFF_SERVICE_UPDATED,
            offering,
            actor,
            organization=offering.staff.organization,
            changes=diff,
            metadata=_offering_metadata(offering),
        )
    sync_service_mirrors([offering.service_id])
    return offering


@transaction.atomic
def remove_offering(*, offering: StaffServiceOffering, actor=None) -> None:
    _audit(
        AuditAction.STAFF_SERVICE_REMOVED,
        offering,
        actor,
        organization=offering.staff.organization,
        metadata=_offering_metadata(offering),
    )
    service_id = offering.service_id
    offering.delete()
    sync_service_mirrors([service_id])


@dataclass(frozen=True)
class OfferingChoice:
    """One service in ``set_staff_offerings``: offered at ``locations`` (``None`` = all the
    person's locations), with optional custom duration and price."""

    service: Service
    locations: tuple[Location, ...] | None = None
    custom_duration_minutes: int | None = None
    custom_price: Decimal | None = None


@transaction.atomic
def set_staff_offerings(*, staff: StaffProfile, choices, actor=None) -> list:
    """Replace everything ``staff`` offers with ``choices`` (a list of ``OfferingChoice``).

    Unchanged offerings are kept as they are; only real changes are written and audited.
    """
    staff = _lock_staff(staff)
    wanted = {}
    for choice in choices:
        _check_custom_values(choice.custom_duration_minutes, choice.custom_price)
        locations = [None] if choice.locations is None else list(choice.locations)
        if not locations:
            continue
        for location in locations:
            _check_offering(staff, choice.service, location)
            wanted[(choice.service.pk, location.pk if location else None)] = (choice, location)

    touched = set()
    for offering in list(staff.offerings.all()):
        key = (offering.service_id, offering.location_id)
        if key not in wanted:
            touched.add(offering.service_id)
            remove_offering(offering=offering, actor=actor)
            continue
        choice, _ = wanted.pop(key)
        changes = {
            name: value
            for name, value in (
                ("custom_duration_minutes", choice.custom_duration_minutes),
                ("custom_price", choice.custom_price),
                ("is_active", True),
            )
            if getattr(offering, name) != value
        }
        if changes:
            update_offering(offering=offering, actor=actor, **changes)
    for choice, location in wanted.values():
        add_offering(
            staff=staff,
            service=choice.service,
            location=location,
            custom_duration_minutes=choice.custom_duration_minutes,
            custom_price=choice.custom_price,
            actor=actor,
        )
    sync_service_mirrors(touched)
    return list(staff.offerings.select_related("service", "location"))


@transaction.atomic
def set_service_providers(*, service: Service, staff_members, actor=None) -> None:
    """The older "assigned staff" list of a service, as offerings.

    Each listed person offers the service at all their locations (unless they already offer
    it somewhere); everyone else stops offering it.
    """
    staff_members = list(staff_members)
    for staff in staff_members:
        if staff.organization_id != service.organization_id:
            raise DomainError("Staff member not found", code="invalid_staff")
    keep = {staff.pk for staff in staff_members}
    for offering in StaffServiceOffering.objects.filter(service=service).exclude(staff_id__in=keep):
        remove_offering(offering=offering, actor=actor)
    offered = set(
        StaffServiceOffering.objects.filter(service=service).values_list("staff_id", flat=True)
    )
    for staff in staff_members:
        if staff.pk not in offered:
            add_offering(staff=staff, service=service, actor=actor)
    sync_service_mirrors([service.pk])
