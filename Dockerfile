FROM python:3.11-slim-bullseye

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (lightweight - no torch!)
COPY requirements_cloud.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && rm -rf /root/.cache

# Copy project
COPY . .

EXPOSE ${PORT}

CMD ["sh", "scripts/docker-entrypoint-web.sh", "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "3600", "core.wsgi:application"]
