#!/bin/sh
set -e

until nc -z ${POSTGRES_HOST:-db} ${POSTGRES_PORT:-5432}; do
  echo "Waiting for PostgreSQL..."
  sleep 1
done

# Only the web container migrates and collects static files; workers just run their command.
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  python manage.py migrate --noinput
  python manage.py collectstatic --noinput
fi

exec "$@"
