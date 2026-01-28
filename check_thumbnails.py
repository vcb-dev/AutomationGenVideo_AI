
import os
import django
import sys

# Setup Django environment
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import ScrapedVideo

def check_thumbnails():
    total_videos = ScrapedVideo.objects.count()
    videos_with_thumbnail = ScrapedVideo.objects.exclude(thumbnail_url='').exclude(thumbnail_url__isnull=True).count()
    
    print(f"Total Videos in DB: {total_videos}")
    print(f"Videos with Thumbnail: {videos_with_thumbnail}")
    
    if total_videos > 0:
        print("\n--- Inspecting Thumbnail Sources for 5 Sample Videos ---")
        samples = ScrapedVideo.objects.all()[:5]
        for v in samples:
            raw = v.raw_data
            print(f"\nVideo ID: {v.video_id}")
            print(f"Saved Thumbnail URL: {v.thumbnail_url}")
            
            # Check raw data sources
            video_meta = raw.get('videoMeta', {})
            cover = video_meta.get('cover') if isinstance(video_meta, dict) else None
            dynamic = video_meta.get('dynamicCover') if isinstance(video_meta, dict) else None
            origin = video_meta.get('originCover') if isinstance(video_meta, dict) else None
            
            print(f"Raw Keys: {list(raw.keys())}")

            if 'videoMeta' in raw:
                meta = raw['videoMeta']
                print(f"Content of videoMeta: {meta}")
                
                # Check directly in the dictionary we just printed
                if isinstance(meta, dict):
                    print(f"  -> cover: {meta.get('cover')}")
                    print(f"  -> dynamicCover: {meta.get('dynamicCover')}")
                    print(f"  -> originCover: {meta.get('originCover')}")
                else:
                    print("  -> videoMeta is NOT a dictionary!")
            else:
                 print("!!! videoMeta KEY IS MISSING !!!")
            
            print("-" * 30)
    else:
        print("No videos found in database to check.")

if __name__ == '__main__':
    check_thumbnails()
