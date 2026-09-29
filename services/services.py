"""Service and category writes: the one place that creates, changes, archives or removes them.

The API, the web app, ``seed_demo`` and future AI agents call these functions, so unique
slugs, the plan's service limit, value checks, same-organization checks and auditing apply the
same way everywhere. Errors are ``DomainError`` (400) or ``ConflictError`` (409) with a stable
``code``.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils.text import slugify

from core.audit import AuditAction, diff_snapshots, record_audit, snapshot
from core.exceptions import ConflictError, DomainError
from crm.models import HEX_COLOR
from locations.models import Location
from organizations.models import Organization
from services.models import Service, ServiceCategory
from staff.services import sync_service_mirrors

SERVICE_FIELDS = frozenset(
    {
        "name",
        "description",
        "category",
        "price",
        "currency",
        "duration_minutes",
        "buffer_before_minutes",
        "buffer_after_minutes",
        "is_active",
        "is_public",
        "is_archived",
        "color",
        "max_advance_days",
        "min_notice_minutes",
        "cancellation_deadline_hours",
        "rescheduling_deadline_hours",
        "capacity",
        "required_provider_type",
        "tax_rate",
        "cancellation_policy",
    }
)
CATEGORY_FIELDS = frozenset({"name", "color", "sort_order"})


def _unique_slug(model, organization, name: str, exclude_pk=None) -> str:
    base = slugify(name)[:140] or "item"
    taken = set(
        model.objects.filter(organization=organization, slug__startswith=base)
        .exclude(pk=exclude_pk)
        .values_list("slug", flat=True)
    )
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _check_plan_limit(organization) -> None:
    from subscriptions.services import enforce_plan_limit

    try:
        enforce_plan_limit(organization, "services")
    except ValueError as error:
        raise ConflictError(str(error), code="plan_limit") from error


def _check_service(service: Service) -> None:
    service.name = (service.name or "").strip()
    if not service.name:
        raise DomainError("A service name is required", code="name_required")
    if not service.duration_minutes or service.duration_minutes <= 0:
        raise DomainError("The duration must be at least one minute", code="invalid_duration")
    if service.price is None or Decimal(service.price) < 0:
        raise DomainError("The price can't be negative", code="invalid_price")
    if not Decimal(0) <= Decimal(service.tax_rate or 0) <= Decimal(100):
        raise DomainError("The tax rate must be between 0 and 100", code="invalid_tax_rate")
    if (service.capacity or 0) < 1:
        raise DomainError("Capacity must be at least 1", code="invalid_capacity")
    if service.category is not None and service.category.organization_id != (
        service.organization_id
    ):
        raise DomainError("Category not found", code="invalid_category")
    service.required_provider_type = (service.required_provider_type or "").strip()


def _check_locations(organization, locations) -> list[Location]:
    locations = list(locations)
    ids = {location.pk for location in locations}
    if Location.objects.filter(organization=organization, pk__in=ids).count() != len(ids):
        raise DomainError("Location not found", code="invalid_location")
    return locations


def _check_fields(fields: dict, allowed) -> None:
    unknown = set(fields) - allowed
    if unknown:
        raise DomainError(f"Unknown fields: {', '.join(sorted(unknown))}", code="invalid")


def _audit(action, target, actor, **kwargs):
    record_audit(action, organization=target.organization, actor=actor, target=target, **kwargs)


# -- Services -------------------------------------------------------------------------------


@transaction.atomic
def create_service(*, organization, actor=None, locations=None, **fields) -> Service:
    """``locations``: where it is offered (empty or None: every location)."""
    _check_fields(fields, SERVICE_FIELDS)
    organization = Organization.objects.select_for_update().get(pk=organization.pk)
    fields.setdefault("currency", organization.currency)
    service = Service(organization=organization, **fields)
    _check_service(service)
    if not service.is_archived:
        _check_plan_limit(organization)
    locations = _check_locations(organization, locations or [])
    service.slug = _unique_slug(Service, organization, service.name)
    service.save()
    service.locations.set(locations)
    _audit(AuditAction.SERVICE_CREATED, service, actor, metadata={"name": service.name})
    return service


@transaction.atomic
def update_service(*, service: Service, actor=None, locations=None, **changes) -> Service:
    """``locations=None`` leaves them unchanged; ``[]`` means every location."""
    _check_fields(changes, SERVICE_FIELDS)
    Organization.objects.select_for_update().get(pk=service.organization_id)
    service = Service.objects.select_for_update().select_related("organization").get(pk=service.pk)
    before = snapshot(service)
    was_archived = service.is_archived
    for name, value in changes.items():
        setattr(service, name, value)
    _check_service(service)
    if was_archived and not service.is_archived:
        _check_plan_limit(service.organization)
    service.save()
    if locations is not None:
        service.locations.set(_check_locations(service.organization, locations))
    diff = diff_snapshots(before, snapshot(service))
    if diff:
        _audit(AuditAction.SERVICE_UPDATED, service, actor, changes=diff)
    if {"required_provider_type", "locations"} & set(diff):
        # Providers who no longer fit drop out of the assigned-staff mirror.
        sync_service_mirrors([service.pk])
    return service


@transaction.atomic
def delete_service(*, service: Service, actor=None) -> None:
    """Delete a service that was never booked; otherwise archive it instead (409 ``in_use``)."""
    service = Service.objects.select_related("organization").get(pk=service.pk)
    _audit(AuditAction.SERVICE_DELETED, service, actor, metadata={"name": service.name})
    try:
        with transaction.atomic():
            service.delete()
    except ProtectedError as error:
        raise ConflictError(
            "This service has appointments. Archive it instead.", code="in_use"
        ) from error


# -- Categories -----------------------------------------------------------------------------


def _check_category(category: ServiceCategory) -> None:
    category.name = (category.name or "").strip()
    if not category.name:
        raise DomainError("A category name is required", code="name_required")
    category.color = (category.color or "").strip().lower()
    if category.color and not HEX_COLOR.regex.match(category.color):
        raise DomainError("Use a hex colour such as #3b82f6", code="invalid_color")
    duplicate = ServiceCategory.objects.filter(
        organization_id=category.organization_id, name__iexact=category.name
    ).exclude(pk=category.pk)
    if duplicate.exists():
        raise ConflictError("A category with this name already exists", code="duplicate")


def _save_category(category: ServiceCategory) -> None:
    """Save; a unique-constraint race (without the lock) is a 409, never a 500."""
    try:
        with transaction.atomic():
            category.save()
    except IntegrityError as error:
        raise ConflictError("A category with this name already exists", code="duplicate") from error


@transaction.atomic
def create_category(*, organization, actor=None, **fields) -> ServiceCategory:
    _check_fields(fields, CATEGORY_FIELDS)
    # Serialize category writes per organization: the name and slug checks below are
    # check-then-insert.
    organization = Organization.objects.select_for_update().get(pk=organization.pk)
    category = ServiceCategory(organization=organization, **fields)
    _check_category(category)
    category.slug = _unique_slug(ServiceCategory, organization, category.name)
    _save_category(category)
    _audit(AuditAction.SERVICE_CATEGORY_CREATED, category, actor, metadata={"name": category.name})
    return category


@transaction.atomic
def update_category(*, category: ServiceCategory, actor=None, **changes) -> ServiceCategory:
    _check_fields(changes, CATEGORY_FIELDS)
    Organization.objects.select_for_update().get(pk=category.organization_id)
    category = ServiceCategory.objects.select_for_update().get(pk=category.pk)
    before = snapshot(category)
    for name, value in changes.items():
        setattr(category, name, value)
    _check_category(category)
    _save_category(category)
    diff = diff_snapshots(before, snapshot(category))
    if diff:
        _audit(AuditAction.SERVICE_CATEGORY_UPDATED, category, actor, changes=diff)
    return category


@transaction.atomic
def delete_category(*, category: ServiceCategory, actor=None) -> None:
    """Its services stay, without a category."""
    _audit(
        AuditAction.SERVICE_CATEGORY_DELETED,
        category,
        actor,
        metadata={"name": category.name, "services": category.services.count()},
    )
    category.delete()
