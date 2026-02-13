import os
import sys
import django
import shutil

# Setup Django environment
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

django.setup()

from video_management.models import IndexedVideo
from django.conf import settings

def clear_all_video_data():
    print("🗑️  Starting cleanup process...")
    
    # 1. Clear IndexedVideo database
    count = IndexedVideo.objects.count()
    IndexedVideo.objects.all().delete()
    print(f"✅ Deleted {count} indexed videos from database.")
    
    # 2. Clear Clip Cache
    cache_dir = getattr(settings, 'VIDEO_CLIP_CACHE_DIR', os.path.join(settings.BASE_DIR, 'media', 'clip_cache'))
    if os.path.exists(cache_dir):
        try:
            # Delete all files in cache dir
            files = os.listdir(cache_dir)
            deleted_files = 0
            for f in files:
                file_path = os.path.join(cache_dir, f)
                try:
                    if os.path.isfile(file_path):
                        os.unlink(file_path)
                        deleted_files += 1
                except Exception as e:
                    print(f"Failed to delete {file_path}: {e}")
            print(f"✅ Cleared {deleted_files} files from cache directory: {cache_dir}")
        except Exception as e:
            print(f"Error clearing cache directory: {e}")
    else:
        print(f"⚠️ Cache directory does not exist: {cache_dir}")

    print("\n✨ SYSTEM CLEANED! You can now retry mixing videos.")
    print("Next step: Go to 'Quản lý Folders' -> 'Scan Folders' to re-index, OR let the system auto-scan specific products.")

if __name__ == '__main__':
    clear_all_video_data()
