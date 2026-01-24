#!/bin/bash

# Unified script to start Django development server
# Usage: ./start.sh [options]
# Options:
#   --python PATH    Use specific Python executable
#   --port PORT      Run on specific port (default: 8000)
#   --no-migrate     Skip migrations

set -e

# Default values
PYTHON_CMD="python3"
PORT="8000"
RUN_MIGRATE=true

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --python)
            PYTHON_CMD="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --no-migrate)
            RUN_MIGRATE=false
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: ./start.sh [--python PATH] [--port PORT] [--no-migrate]"
            exit 1
            ;;
    esac
done

echo "=========================================="
echo "Starting Django Development Server"
echo "=========================================="

# Navigate to script directory
cd "$(dirname "$0")"

# Check if Python is available
if ! command -v "$PYTHON_CMD" &> /dev/null; then
    echo "Error: $PYTHON_CMD is not installed or not in PATH"
    exit 1
fi

echo "Using Python: $PYTHON_CMD"
echo "Python version: $($PYTHON_CMD --version)"
echo ""

# Activate virtual environment if exists
if [ -d "venv" ]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
    PYTHON_CMD="python"
fi

# Check if Django is installed
echo "Checking Django installation..."
if ! $PYTHON_CMD -c "import django" 2>/dev/null; then
    echo "Django not found. Installing dependencies..."
    $PYTHON_CMD -m pip install -r requirements.txt
else
    DJANGO_VERSION=$($PYTHON_CMD -c "import django; print(django.get_version())")
    echo "Django $DJANGO_VERSION found"
fi

# Run migrations
if [ "$RUN_MIGRATE" = true ]; then
    echo ""
    echo "Running migrations..."
    $PYTHON_CMD manage.py migrate --noinput
fi

# Check if port is available
if command -v lsof &> /dev/null; then
    if lsof -Pi :$PORT -sTCP:LISTEN -t > /dev/null 2>&1; then
        echo ""
        echo "Warning: Port $PORT is already in use!"
        read -p "Kill the process and continue? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            lsof -ti :$PORT | xargs kill -9
            echo "Process killed."
        else
            exit 1
        fi
    fi
fi

# Start server
echo ""
echo "=========================================="
echo "Server starting on http://localhost:$PORT"
echo "Press Ctrl+C to stop"
echo "=========================================="
echo ""

$PYTHON_CMD manage.py runserver 0.0.0.0:$PORT
