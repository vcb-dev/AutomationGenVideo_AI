FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WORKDIR=/app

# Install system dependencies
# libgl1-mesa-glx and libglib2.0-0 are required for cv2
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
COPY requirements-duplicate-detection.txt .

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r requirements-duplicate-detection.txt

# Copy project
COPY . .

# Make scripts executable
RUN chmod +x manage.py
RUN chmod +x start.sh

# Run with Gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "core.wsgi:application"]
