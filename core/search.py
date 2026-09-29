"""Organization-wide search, shared by the API (``/api/v1/search/``) and the app header.

Each section is filtered by the caller's capabilities through the same selectors as browsing,
so search can never show more than the lists would. Sections the caller may not see are left
out of the result.
"""

from __future__ import annotations

from django.db.models import Q

from bookings.models import Customer
from bookings.selectors import bookings_visible_to
from crm.selectors import customers_visible_to, search_filter
from organizations.permissions import Capability
from organizations.tenancy import resolve_tenant
from services.models import Service
from staff.models import StaffProfile

MIN_QUERY_LENGTH = 2


def organization_search(request, query: str, *, limit: int) -> dict[str, list]:
    """Model instances per section: customers, appointments, and (with the capability)
    staff and services. The request must have a tenant context."""
    tenant = resolve_tenant(request)
    organization = tenant.organization
    results = {}

    customers = customers_visible_to(request).exclude(status=Customer.Status.ANONYMIZED)
    results["customers"] = list(
        search_filter(customers, query).order_by("last_name", "first_name")[:limit]
    )

    appointments = bookings_visible_to(request).filter(
        Q(reference__icontains=query) | Q(customer_name__icontains=query)
    )
    results["appointments"] = list(appointments.order_by("-start_datetime")[:limit])

    if tenant.has(Capability.STAFF_VIEW):
        staff = StaffProfile.objects.filter(organization=organization).select_related("user")
        for term in query.split():
            staff = staff.filter(
                Q(user__first_name__icontains=term)
                | Q(user__last_name__icontains=term)
                | Q(job_title__icontains=term)
            )
        results["staff"] = list(staff.order_by("user__first_name", "user__last_name")[:limit])

    if tenant.has(Capability.SERVICES_VIEW):
        services = Service.objects.filter(
            organization=organization, is_archived=False, name__icontains=query
        )
        results["services"] = list(services.order_by("name")[:limit])

    return results
