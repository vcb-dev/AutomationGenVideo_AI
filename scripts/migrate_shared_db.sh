#!/bin/sh
# Run ONCE from Mac to sync django_migrations on shared Supabase DB.
# Usage: sh scripts/migrate_shared_db.sh
set -e
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

echo ">>> Installing minimal deps..."
.venv/bin/pip install -q django psycopg2-binary django-environ djangorestframework django-cors-headers celery redis

echo ">>> Faking migrations (tables already exist on Supabase)..."
.venv/bin/python manage.py migrate contenttypes --fake
.venv/bin/python manage.py migrate auth --fake
.venv/bin/python manage.py migrate admin --fake
.venv/bin/python manage.py migrate sessions --fake
.venv/bin/python manage.py migrate video_management --fake

echo ">>> Verifying..."
.venv/bin/python manage.py check

echo ">>> Done. Start ai-server on NAS (migrate skipped on container start)."
