"""Organization-wide search: ``GET /api/v1/search/?q=...&limit=5``.

One query box for the app header (M2.6). Each section is filtered by the caller's
capabilities through the same selectors as the list endpoints, so search can never show
more than browsing would. Sections the caller may not see are left out of the response.
"""

from django.db.models import Q
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.models import Customer
from bookings.selectors import bookings_visible_to
from core.permissions import IsOrganizationMember
from crm.selectors import customers_visible_to, search_filter
from organizations.permissions import Capability
from organizations.tenancy import resolve_tenant
from services.models import Service
from staff.models import StaffProfile

DEFAULT_LIMIT, MAX_LIMIT = 5, 20


class GlobalSearchView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]

    def get(self, request):
        query = request.query_params.get("q", "").strip()
        if len(query) < 2:
            raise ValidationError({"q": "Enter at least 2 characters."})
        try:
            limit = min(max(int(request.query_params.get("limit", DEFAULT_LIMIT)), 1), MAX_LIMIT)
        except ValueError as error:
            raise ValidationError({"limit": "Must be a number."}) from error

        tenant = resolve_tenant(request)
        organization = tenant.organization
        results = {}

        customers = customers_visible_to(request).exclude(status=Customer.Status.ANONYMIZED)
        results["customers"] = [
            {
                "id": str(c.pk),
                "display_name": c.display_name,
                "email": c.email,
                "phone": c.phone,
            }
            for c in search_filter(customers, query).order_by("last_name", "first_name")[:limit]
        ]

        appointments = bookings_visible_to(request).filter(
            Q(reference__icontains=query) | Q(customer_name__icontains=query)
        )
        results["appointments"] = [
            {
                "id": str(b.pk),
                "reference": b.reference,
                "customer_name": b.customer_name,
                "service": b.service.name,
                "start_datetime": b.start_datetime.isoformat(),
                "status": b.status,
            }
            for b in appointments.order_by("-start_datetime")[:limit]
        ]

        if tenant.has(Capability.STAFF_VIEW):
            staff = StaffProfile.objects.filter(organization=organization).select_related("user")
            for term in query.split():
                staff = staff.filter(
                    Q(user__first_name__icontains=term)
                    | Q(user__last_name__icontains=term)
                    | Q(job_title__icontains=term)
                )
            results["staff"] = [
                {
                    "id": str(s.pk),
                    "name": s.user.get_full_name(),
                    "job_title": s.job_title,
                    "is_active": s.is_active,
                }
                for s in staff.order_by("user__first_name", "user__last_name")[:limit]
            ]

        if tenant.has(Capability.SERVICES_VIEW):
            services = Service.objects.filter(
                organization=organization, is_archived=False, name__icontains=query
            )
            results["services"] = [
                {"id": str(s.pk), "name": s.name, "is_active": s.is_active}
                for s in services.order_by("name")[:limit]
            ]

        return Response({"query": query, "results": results})
