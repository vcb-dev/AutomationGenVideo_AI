
import os
import django
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import TrackedChannel, ScrapedVideo, SearchHistory, Platform

target_platform = Platform.FACEBOOK

print(f"Start deleting data for platform: {target_platform}...")

# Delete Channels
deleted_channels_count, _ = TrackedChannel.objects.filter(platform=target_platform).delete()
print(f"Deleted Channels: {deleted_channels_count}")

# Delete Videos
deleted_videos_count, _ = ScrapedVideo.objects.filter(platform=target_platform).delete()
print(f"Deleted Videos: {deleted_videos_count}")

# Delete Search History
deleted_history_count, _ = SearchHistory.objects.filter(platform=target_platform).delete()
print(f"Deleted Search History: {deleted_history_count}")

print("Facebook Data Cleared Successfully")
