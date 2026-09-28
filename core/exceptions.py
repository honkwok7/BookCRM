"""Domain errors raised by the service layer and their HTTP mapping.

Services raise these instead of bare exceptions; every entry point (API, web views, future AI
agents) then reports them consistently. ``DomainError`` subclasses ``ValueError`` so older
callers that catch ``ValueError`` keep working.
"""

from rest_framework import status
from rest_framework.views import exception_handler as drf_exception_handler


class DomainError(ValueError):
    """A business rule was violated by the request (HTTP 400)."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class ConflictError(DomainError):
    """The request conflicts with the current state, e.g. a taken slot (HTTP 409)."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


def api_exception_handler(exc, context):
    if isinstance(exc, DomainError):
        from rest_framework.response import Response

        return Response({"detail": exc.message, "code": exc.code}, status=exc.status_code)
    return drf_exception_handler(exc, context)
