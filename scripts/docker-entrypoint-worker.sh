#!/bin/sh
# Install deps before celery worker/beat start.
set -e
cd /app

echo ">>> Installing Python dependencies..."
pip install --no-cache-dir -r requirements_cloud.txt

echo ">>> Starting worker..."
exec "$@"
