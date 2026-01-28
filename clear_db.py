
import os
import django
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import TrackedChannel, ScrapedVideo, SearchHistory

print("Start deleting...")

deleted_channels = TrackedChannel.objects.all().delete()
print(f"Deleted Channels: {deleted_channels}")

deleted_videos = ScrapedVideo.objects.all().delete()
print(f"Deleted Videos: {deleted_videos}")

deleted_history = SearchHistory.objects.all().delete()
print(f"Deleted Search History: {deleted_history}")

print("DB Cleared Successfully")
