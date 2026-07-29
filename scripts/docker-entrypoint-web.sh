#!/bin/sh
# NAS-friendly startup: install deps, optional migrate, then start app.
set -e
cd /app

echo ">>> Installing Python dependencies..."
pip install --no-cache-dir -r requirements_cloud.txt

if [ "${RUN_MIGRATE:-false}" = "true" ]; then
  fake_initial_migrations() {
    echo ">>> Existing tables detected — marking initial migrations as applied..."
    python manage.py migrate contenttypes 0001 --fake || true
    python manage.py migrate auth 0001 --fake || true
    python manage.py migrate admin 0001 --fake || true
    python manage.py migrate sessions 0001 --fake || true
    python manage.py migrate authtoken 0001 --fake || true
    python manage.py migrate video_management 0001 --fake || true
  }

  echo ">>> Running database migrations..."
  if ! python manage.py migrate --noinput --fake-initial; then
    fake_initial_migrations
    if ! python manage.py migrate --noinput --fake-initial; then
      echo "WARNING: migrate failed — check DB manually"
    fi
  fi
else
  echo ">>> Skipping migrate (set RUN_MIGRATE=true to enable)"
fi

echo ">>> Starting application..."
exec "$@"
