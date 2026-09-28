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
- Set `DJANGO_SECURE_SSL_REDIRECT=True`.
- Set secure cookie flags and trusted CSRF origins.
