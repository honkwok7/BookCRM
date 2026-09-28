from django.urls import path

from dashboard.views import OrganizationDashboardSummaryView

urlpatterns = [
    path(
        "dashboard/summary/", OrganizationDashboardSummaryView.as_view(), name="dashboard-summary"
    ),
]
