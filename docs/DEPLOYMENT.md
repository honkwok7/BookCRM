# Deployment

## Required environment variables
- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS`
- `DATABASE_URL`
- `REDIS_URL`
- `CELERY_BROKER_URL`
- `CELERY_RESULT_BACKEND`
- SMTP settings (`EMAIL_*`, `DEFAULT_FROM_EMAIL`)
- `CACHE_URL`: a Redis database for the shared cache (for example `redis://redis:6379/1`).
  Rate-limit counters and API throttles live in the cache; without a shared cache each
  worker process counts on its own, so the limits are several times looser.

Optional: `SITE_NAME` (product name in the UI and account emails, default `BookCRM`),
`WEB_LOGIN_RATE` and `WEB_PASSWORD_RESET_RATE` (`<attempts>/<seconds>`, defaults `10/900` and
`5/3600`).

## Static files and the stylesheet
- Every script and stylesheet is served by the app itself (WhiteNoise). No CDN is used, which
  is what lets the Content Security Policy allow only `'self'`.
- The stylesheet `static/dist/app.css` is built from `static/src/app.css` and the templates by
  the Tailwind standalone CLI (no Node.js): `python manage.py tailwind build`, or
  `python manage.py tailwind watch` while editing templates. The CLI version is pinned and
  its download is checked against a SHA-256 checksum.
- The built file is committed, so deployments need no build step. CI rebuilds it and fails if
  the committed file is out of date.
- Upgrading htmx or Alpine: download the new file into `static/vendor/`, compare it with the
  npm package (`npm view <package>@<version> dist.integrity`), and update the file names in
  `templates/base.html`.

## Container entrypoint
`entrypoint.sh` waits for PostgreSQL and then runs the container's command (`exec "$@"`).
Set `RUN_MIGRATIONS=1` on exactly one service (the web container in `docker-compose.yml`) to run
`migrate` and `collectstatic` before start. Worker and beat containers must not set it.

## Process commands
- Web: `gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3`
- Worker: `celery -A config worker -l info`
- Beat: `celery -A config beat -l info`
- Migrations: `python manage.py migrate`
- Static: `python manage.py collectstatic --noinput`

## Health checks
- Liveness: `/health/`
- Readiness: `/ready/`

## PostgreSQL and Redis
- Provision PostgreSQL with backups.
- Provision Redis for Celery broker/result backend.

## HTTPS and domain
- Enable TLS termination at proxy/load balancer.
- Set `TRUSTED_PROXY_COUNT` to the number of reverse proxies (usually `1`). Only then does
  Django trust `X-Forwarded-Proto` (HTTPS detection) and `X-Forwarded-For` (client IP).
  Leaving it at `0` behind a TLS-terminating proxy makes `DJANGO_SECURE_SSL_REDIRECT` loop.
- Set `SITE_URL` to the public `https://` address; emailed links are built from it.
- Set `DJANGO_SECURE_SSL_REDIRECT=True`.
- Set secure cookie flags and trusted CSRF origins.
