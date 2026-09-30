from django.urls import path

from dashboard import web_views

urlpatterns = [
    path("home/", web_views.home, name="home"),
    path("app/", web_views.home),
    path("app/dashboard/", web_views.AppDashboardView.as_view(), name="app-dashboard"),
    path("app/components/", web_views.ComponentGalleryView.as_view(), name="app-components"),
    path("portal/", web_views.PortalHomeView.as_view(), name="portal-home"),
    path("saas/", web_views.SaasDashboardView.as_view(), name="saas-dashboard"),
]
