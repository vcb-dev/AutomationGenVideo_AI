# Optional Celery import - only import if celery is available
try:
    from .celery import app as celery_app
    __all__ = ('celery_app',)
except ImportError:
    # Celery is optional, app can run without it
    celery_app = None
    __all__ = ()
