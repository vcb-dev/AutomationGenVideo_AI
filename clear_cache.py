import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import FacebookPageCache
try:
    cache = FacebookPageCache.objects.get(username='61580182263005')
    cache.delete()
    print("Deleted old cache successfully!")
except Exception as e:
    print("No cache found or error:", e)
