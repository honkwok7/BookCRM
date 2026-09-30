from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerSplitView,
)

from core.csp import api_docs_csp

urlpatterns = [
    path("", include("core.web_urls")),
    path("", include("bookings.web_urls")),
    path("", include("accounts.web_urls")),
    path("", include("organizations.web_urls")),
    path("", include("dashboard.web_urls")),
    path("", include("crm.web_urls")),
    path("", include("locations.web_urls")),
    path("", include("staff.web_urls")),
    path("", include("services.web_urls")),
    path("", include("portal.urls")),
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    # Swagger's "split" view serves its start-up script as a file, not inline (CSP).
    path(
        "api/docs/",
        api_docs_csp(SpectacularSwaggerSplitView.as_view(url_name="schema")),
        name="swagger-ui",
    ),
    path("api/redoc/", api_docs_csp(SpectacularRedocView.as_view(url_name="schema")), name="redoc"),
    path("api/", include("accounts.urls")),
    path("api/", include("organizations.urls")),
    path("api/", include("dashboard.urls")),
    path("api/v1/", include("api.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
