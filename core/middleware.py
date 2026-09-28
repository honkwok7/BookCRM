from __future__ import annotations

from threading import local

_request_local = local()


def get_current_request():
    return getattr(_request_local, "request", None)


class RequestAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _request_local.request = request
        try:
            return self.get_response(request)
        finally:
            # Never let a finished request leak into later work on the same thread
            # (audit rows would otherwise be attributed to the wrong user).
            _request_local.request = None
