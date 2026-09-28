"""CRM customer reads. Every function takes the organization explicitly: no global lookups."""

from __future__ import annotations

from django.db.models import Count, Exists, Max, Min, OuterRef, Q, QuerySet, Sum
from django.utils import timezone

from bookings.models import Booking, Customer
from bookings.services import ACTIVE_BOOKING_STATUSES
from crm.models import CustomerActivity, CustomerNote, Tag


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
    for term in search.split():
        customers = customers.filter(
            Q(first_name__icontains=term)
            | Q(last_name__icontains=term)
            | Q(preferred_name__icontains=term)
            | Q(email__icontains=term)
            | Q(phone__icontains=term)
        )
    return customers.order_by("last_name", "first_name", "created_at")


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


def customer_timeline(customer, *, include_internal: bool) -> QuerySet[CustomerActivity]:
    activities = CustomerActivity.objects.filter(
        organization_id=customer.organization_id, customer=customer
    ).select_related("actor")
    if not include_internal:
        activities = activities.filter(internal=False)
    return activities
