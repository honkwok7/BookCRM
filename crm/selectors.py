"""CRM customer reads. Every function takes the organization explicitly: no global lookups."""

from __future__ import annotations

import re

from django.db.models import Count, Exists, Max, Min, OuterRef, Q, QuerySet, Sum
from django.utils import timezone

from bookings.models import Booking, Customer
from bookings.services import ACTIVE_BOOKING_STATUSES
from crm.models import CustomerActivity, CustomerNote, Tag
from organizations.models import OrganizationRole
from organizations.permissions import Capability
from organizations.tenancy import resolve_tenant


def get_customer_for_org(organization, customer_id) -> Customer | None:
    return Customer.objects.filter(organization=organization, pk=customer_id).first()


def list_customers(
    organization,
    *,
    search: str = "",
    status: str | None = None,
    tag=None,
    include_anonymized=False,
) -> QuerySet[Customer]:
    customers = (
        Customer.objects.filter(organization=organization)
        .select_related("assigned_staff__user", "preferred_staff__user")
        .prefetch_related("tag_set")
    )
    if tag is not None:
        customers = customers.filter(customer_tags__tag=tag)
    if status:
        customers = customers.filter(status=status)
    elif not include_anonymized:
        customers = customers.exclude(status=Customer.Status.ANONYMIZED)
    return search_filter(customers, search).order_by("last_name", "first_name", "created_at")


PHONE_LIKE = re.compile(r"^[\d\s().+\-]+$")


def search_filter(customers: QuerySet[Customer], query: str) -> QuerySet[Customer]:
    """Filter customers by a free-text query.

    - A phone-looking query ("(416) 555-0101", "+1 416 555 0101") matches the digits of
      phone/secondary_phone in any format; an 11-digit number starting with 1 also matches
      the 10-digit form.
    - Otherwise every word must match a first, last or preferred name or the email.
    Both paths are served by the trigram indexes (migration bookings/0007) on PostgreSQL.
    """
    query = (query or "").strip()
    if not query:
        return customers
    digits = Customer.digits(query)
    if PHONE_LIKE.match(query) and len(digits) >= 3:
        condition = Q(phone_search__contains=digits)
        if len(digits) == 11 and digits.startswith("1"):
            condition |= Q(phone_search__contains=digits[1:])
        return customers.filter(condition)
    for term in query.split():
        customers = customers.filter(
            Q(first_name__icontains=term)
            | Q(last_name__icontains=term)
            | Q(preferred_name__icontains=term)
            | Q(email__icontains=term)
        )
    return customers


# -- Who sees what -------------------------------------------------------------------------


def customers_visible_to(request) -> QuerySet[Customer]:
    """Customers the requesting team member may read.

    - ``customers.view``: every customer of the organization.
    - Providers (staff role) without it: customers assigned to them, or with whom they have
      (had) an appointment. Read-only; writes still need ``customers.manage``.
    - Everyone else: none.
    """
    tenant = resolve_tenant(request)
    if tenant is None:
        return Customer.objects.none()
    customers = Customer.objects.filter(organization=tenant.organization)
    if tenant.has(Capability.CUSTOMERS_VIEW):
        return customers
    if tenant.role == OrganizationRole.STAFF:
        user = request.user
        own_bookings = Booking.objects.filter(
            organization=tenant.organization, staff__user=user
        ).values("customer_id")
        return customers.filter(Q(assigned_staff__user=user) | Q(pk__in=own_bookings))
    return Customer.objects.none()


def can_read_internal_notes(request) -> bool:
    tenant = resolve_tenant(request)
    return tenant is not None and tenant.has(Capability.CUSTOMERS_NOTES_PRIVATE)


def notes_visible_to(request) -> QuerySet[CustomerNote]:
    """Notes the requesting team member may read.

    With ``customers.notes.private``: all notes of the organization. Otherwise:
    customer-visible notes plus the user's own notes, on customers they can see.
    """
    tenant = resolve_tenant(request)
    if tenant is None:
        return CustomerNote.objects.none()
    notes = CustomerNote.objects.filter(organization=tenant.organization).select_related(
        "author", "customer"
    )
    if can_read_internal_notes(request):
        return notes
    return notes.filter(
        Q(visibility=CustomerNote.Visibility.CUSTOMER_VISIBLE) | Q(author=request.user),
        customer__in=customers_visible_to(request),
    )


def customer_stats(customer: Customer) -> dict:
    """Computed from appointments on every call (no denormalized counters to drift).

    Cancellations caused by a reschedule are not counted as cancellations: the appointment
    moved, it was not called off.
    """
    now = timezone.now()
    bookings = Booking.objects.filter(
        organization=customer.organization, customer=customer
    ).annotate(moved=Exists(Booking.objects.filter(rescheduled_from=OuterRef("pk"))))
    completed = Q(status=Booking.Status.COMPLETED)
    upcoming = Q(status__in=ACTIVE_BOOKING_STATUSES, start_datetime__gte=now)
    stats = bookings.aggregate(
        total_appointments=Count("id", filter=Q(moved=False)),
        completed=Count("id", filter=completed),
        cancelled=Count("id", filter=Q(status=Booking.Status.CANCELLED, moved=False)),
        no_shows=Count("id", filter=Q(status=Booking.Status.NO_SHOW)),
        upcoming=Count("id", filter=upcoming),
        first_visit=Min("start_datetime", filter=completed),
        last_visit=Max("start_datetime", filter=completed),
        next_appointment=Min("start_datetime", filter=upcoming),
        lifetime_value=Sum("price_snapshot", filter=completed),
    )
    stats["lifetime_value"] = stats["lifetime_value"] or 0
    return stats


def list_tags(organization) -> QuerySet[Tag]:
    """The organization's tags with how many (non-anonymized) customers carry each."""
    return Tag.objects.filter(organization=organization).annotate(
        customer_count=Count(
            "customer_tags", filter=~Q(customer_tags__customer__status="anonymized")
        )
    )


# -- Notes and timeline --------------------------------------------------------------------
# Visibility is enforced here, not by callers: without ``include_internal`` (i.e. without the
# customers.notes.private capability) internal notes and their timeline entries never appear.


def team_notes(organization, *, include_internal: bool, customer=None) -> QuerySet[CustomerNote]:
    notes = CustomerNote.objects.filter(organization=organization).select_related(
        "author", "customer"
    )
    if customer is not None:
        notes = notes.filter(customer=customer)
    if not include_internal:
        notes = notes.filter(visibility=CustomerNote.Visibility.CUSTOMER_VISIBLE)
    return notes


def notes_for_customer(customer) -> QuerySet[CustomerNote]:
    """What the customer themselves may read (portal): customer-visible notes only."""
    return CustomerNote.objects.filter(
        organization_id=customer.organization_id,
        customer=customer,
        visibility=CustomerNote.Visibility.CUSTOMER_VISIBLE,
    )


def last_visit_annotation():
    return Max("bookings__start_datetime", filter=Q(bookings__status=Booking.Status.COMPLETED))


def customer_timeline(customer, *, include_internal: bool) -> QuerySet[CustomerActivity]:
    activities = CustomerActivity.objects.filter(
        organization_id=customer.organization_id, customer=customer
    ).select_related("actor")
    if not include_internal:
        activities = activities.filter(internal=False)
    return activities
