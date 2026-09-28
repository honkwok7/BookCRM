from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsOrganizationManagerOrOwner
from dashboard.selectors import organization_dashboard_summary
from organizations.selectors import get_request_organization


class OrganizationDashboardSummaryView(APIView):
	permission_classes = [permissions.IsAuthenticated, IsOrganizationManagerOrOwner]

	def get(self, request, *args, **kwargs):
		organization = get_request_organization(request)
		if organization is None:
			return Response({"detail": "Organization not found"}, status=400)
		return Response(organization_dashboard_summary(organization))
