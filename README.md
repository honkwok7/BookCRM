# Schedula

Production-ready multi-tenant appointment booking SaaS built with Django, DRF, Celery, PostgreSQL, Redis, and a hybrid web/API architecture.

## Live demo
Coming soon.

## Screenshots
See `docs/SCREENSHOTS.md`.

## Main features
- Multi-tenant organizations with strict data isolation
- Email-first custom user model and JWT authentication
- Organization memberships, roles, and invitation workflow
- Service catalog and staff assignment
- Weekly availability, exceptions, holidays, and time off
- Concurrency-safe booking engine with overlap protection
- Waitlist and booking activity/status history
- Notification logs and async email delivery via Celery
- Plan and subscription foundations with limit enforcement hooks
- Versioned API under `/api/v1/`
- OpenAPI, Swagger UI, and ReDoc
- Health and readiness endpoints
- Dockerized local stack with PostgreSQL + Redis + worker + beat

## Technology stack
- Python 3.14
- Django 6
- Django REST Framework
- Simple JWT
- drf-spectacular
- PostgreSQL (primary)
- Redis, Celery, Celery Beat
- HTMX, Alpine.js, Tailwind CSS, Chart.js
- pytest, factory_boy, coverage
- Ruff, Black, isort, pre-commit

## Architecture overview
See:
- `docs/ARCHITECTURE.md`
- `docs/BOOKING_ENGINE.md`
- `docs/CRM.md`
- `docs/LOCATIONS.md`
- `docs/SERVICES.md`
- `docs/STAFF.md`
- `docs/API.md`

## Multi-tenant architecture
Tenant boundary is `Organization`. Every tenant-owned model is organization-scoped and filtered through organization-aware selectors and permission checks.

## Project structure
- `accounts` auth and user lifecycle
- `organizations` tenant model, memberships, invitations
- `services` categories and services
- `staff` staff profiles
- `scheduling` availability/holiday/time-off
- `bookings` booking engine, customers, waitlist
- `notifications` notification logs and email tasks
- `subscriptions` plans and subscriptions
- `dashboard` analytics selectors and endpoints
- `core` shared models, health endpoints, audit helpers
- `api` v1 router composition

## Local installation
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

## Docker installation
```bash
cp .env.example .env
docker compose up --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_demo
```

## Environment variables
Use `.env.example` as baseline. Key variables:
- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `DATABASE_URL`
- `REDIS_URL`
- `CELERY_BROKER_URL`
- `CELERY_RESULT_BACKEND`
- `EMAIL_*`
- `DEFAULT_FROM_EMAIL`
- `APP_BASE_URL`

## Database setup
Primary database is PostgreSQL (`DATABASE_URL`).
SQLite remains available as fallback when `DATABASE_URL` is omitted.

## Running Celery
```bash
celery -A config worker -l info
celery -A config beat -l info
```

## Tests
```bash
pytest
pytest --cov=. --cov-report=html --cov-report=term
```

## API docs
- Schema: `/api/schema/`
- Swagger: `/api/docs/`
- ReDoc: `/api/redoc/`

## Demo credentials
After `python manage.py seed_demo` (idempotent; safe to re-run). It only runs with
`DJANGO_DEBUG=True` (as in `.env.example`): these passwords are public, so never seed a
production database.

| Who | Email | Password |
|---|---|---|
| Platform admin | `admin@bookcrm.local` | `Admin12345!` |
| Harmony Wellness Centre: owner / manager / receptionist | `owner@harmony.local`, `manager@harmony.local`, `reception@harmony.local` | `Demo12345!` |
| Harmony: massage therapist / chiropractor | `massage@harmony.local`, `chiro@harmony.local` | `Demo12345!` |
| Serenity Spa: owner / esthetician | `owner@serenity.local`, `esthetician@serenity.local` | `Demo12345!` |

The customer `alex@example.test` exists in both organizations as two separate records,
which demonstrates tenant isolation.

Development-only credentials. Change immediately outside local/demo.

## Deployment
See `docs/DEPLOYMENT.md`.

## Security
See `SECURITY.md` and `docs/SECURITY.md`.

## Roadmap
- Billing provider integration (Stripe adapters)
- Rich calendar UI (week/day/month)
- Advanced reminder strategies and SMS channels

## Contributing
See `CONTRIBUTING.md`.

## License
MIT
