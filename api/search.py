"""Organization-wide search: ``GET /api/v1/search/?q=...&limit=5``.

The sections and their access rules live in ``core.search.organization_search``, which the
app header's search box uses too.
"""

from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsOrganizationMember
from core.search import MIN_QUERY_LENGTH, organization_search

DEFAULT_LIMIT, MAX_LIMIT = 5, 20


class GlobalSearchView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]

    def get(self, request):
        query = request.query_params.get("q", "").strip()
        if len(query) < MIN_QUERY_LENGTH:
            raise ValidationError({"q": "Enter at least 2 characters."})
        try:
            limit = min(max(int(request.query_params.get("limit", DEFAULT_LIMIT)), 1), MAX_LIMIT)
        except ValueError as error:
            raise ValidationError({"limit": "Must be a number."}) from error

        found = organization_search(request, query, limit=limit)
        results = {
            "customers": [
                {
                    "id": str(c.pk),
                    "display_name": c.display_name,
                    "email": c.email,
                    "phone": c.phone,
                }
                for c in found["customers"]
            ],
            "appointments": [
                {
                    "id": str(b.pk),
                    "reference": b.reference,
                    "customer_name": b.customer_name,
                    "service": b.service.name,
                    "start_datetime": b.start_datetime.isoformat(),
                    "status": b.status,
                }
                for b in found["appointments"]
            ],
        }
        if "staff" in found:
            results["staff"] = [
                {
                    "id": str(s.pk),
                    "name": s.user.get_full_name(),
                    "job_title": s.job_title,
                    "is_active": s.is_active,
                }
                for s in found["staff"]
            ]
        if "services" in found:
            results["services"] = [
                {"id": str(s.pk), "name": s.name, "is_active": s.is_active}
                for s in found["services"]
            ]
        return Response({"query": query, "results": results})
