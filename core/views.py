from django.db import connections
from django.http import JsonResponse


def healthcheck_view(request):
    return JsonResponse({"status": "ok"})


def readiness_view(request):
    errors = []
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # pragma: no cover
        errors.append(str(exc))

    if errors:
        return JsonResponse({"status": "error", "errors": errors}, status=503)
    return JsonResponse({"status": "ready"})
