"""What a signed-in customer may see in the portal (M5.4), one organization at a time.

A customer "belongs" to an organization through a Customer record linked to their account, or
(once their email is verified) through bookings made with their email. Everything below is
filtered by one organization, so the same email at two businesses never mixes: the portal for
one never shows the other's appointments or details.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from bookings.models import Booking, Customer
from bookings.selectors import bookings_for_customer_account
from organizations.models import Organization


def _customer_filter(user) -> Q:
    condition = Q(user=user)
    if user.email_verified and user.email:
        condition |= Q(email__iexact=user.email)
    return condition


def portal_organizations(user) -> QuerySet[Organization]:
    """Active organizations where ``user`` is a customer (a linked record or their bookings)."""
    customer_orgs = Customer.objects.filter(_customer_filter(user)).exclude(
        status=Customer.Status.ANONYMIZED
    )
    booking_orgs = bookings_for_customer_account(user).values("organization_id")
    return (
        Organization.objects.filter(is_active=True, is_suspended=False)
        .filter(Q(pk__in=customer_orgs.values("organization_id")) | Q(pk__in=booking_orgs))
        .order_by("name")
    )


def portal_organization(user, slug: str) -> Organization | None:
    return portal_organizations(user).filter(slug=slug).first()


def portal_bookings(user, organization) -> QuerySet[Booking]:
    """The customer's appointments at this organization only."""
    return (
        bookings_for_customer_account(user)
        .filter(organization=organization)
        .select_related("service", "staff__user", "location", "customer")
    )


def portal_customer(user, organization) -> Customer | None:
    """The customer record their details live in here: the one linked to the account, else
    (verified email) the most recent one with their email. None: nothing to edit yet."""
    customers = (
        Customer.objects.filter(organization=organization)
        .filter(_customer_filter(user))
        .exclude(status=Customer.Status.ANONYMIZED)
    )
    return (
        customers.filter(user=user).order_by("-created_at").first()
        or customers.order_by("-created_at").first()
    )


def portal_form_assignments(user, organization):
    """Forms given to the customer here (M6.2), through any of their customer records."""
    from customer_forms.models import FormAssignment

    customers = (
        Customer.objects.filter(organization=organization)
        .filter(_customer_filter(user))
        .exclude(status=Customer.Status.ANONYMIZED)
    )
    return FormAssignment.objects.filter(
        organization=organization, customer__in=customers
    ).select_related("template", "version", "organization", "booking")
