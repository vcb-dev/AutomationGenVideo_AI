import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import IndexedVideo, VideoClipCache

def clear_all_indexes():
    print("🗑️ Cleaning up IndexedVideo and VideoClipCache...")
    
    # 1. Clear Cache first
    deleted_cache, _ = VideoClipCache.objects.all().delete()
    print(f"✅ Deleted {deleted_cache} cached clips")
    
    # 2. Clear Indexed Videos
    deleted_videos, _ = IndexedVideo.objects.all().delete()
    print(f"✅ Deleted {deleted_videos} indexed videos")
    
    print("🎉 All indexes cleared! System is clean.")

if __name__ == "__main__":
    clear_all_indexes()
