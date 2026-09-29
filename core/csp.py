"""Content Security Policy exceptions.

The site-wide policy (``SECURE_CSP`` in settings) allows only self-hosted scripts and styles.
The third-party API documentation pages (Swagger UI, Redoc) inject inline styles, and Redoc
runs an inline start-up script, loads Google Fonts and uses blob: workers, so those two views
get this looser policy. They show only the public API schema, no tenant data.
"""

from django.utils.csp import CSP
from django.views.decorators.csp import csp_override

API_DOCS_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE, "https://fonts.googleapis.com"],
    "font-src": [CSP.SELF, "https://fonts.gstatic.com"],
    "img-src": [CSP.SELF, "data:", "https:"],
    "worker-src": [CSP.SELF, "blob:"],
    "connect-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
}

api_docs_csp = csp_override(API_DOCS_CSP)
