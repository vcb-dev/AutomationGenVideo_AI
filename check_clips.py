
import os
import django
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import VideoClipCache

print(f"Total Cached Clips: {VideoClipCache.objects.count()}")
