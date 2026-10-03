"""Container health check for the web service (docker-compose.prod.yml).

It calls /ready/ the way the reverse proxy does: with a host Django accepts and
X-Forwarded-Proto: https, so the HTTPS redirect doesn't bounce it.
"""

import os
import sys
import urllib.request

host = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",")[0].strip()
request = urllib.request.Request(
    "http://127.0.0.1:8000/ready/",
    headers={"Host": host, "X-Forwarded-Proto": "https"},
)
try:
    with urllib.request.urlopen(request, timeout=4) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception as error:  # noqa: BLE001 - any failure means unhealthy
    print(error, file=sys.stderr)
    sys.exit(1)
