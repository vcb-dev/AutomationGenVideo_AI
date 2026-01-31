
import os
import django
import sys
import json

# Setup Django environment
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import ScrapedVideo, Platform

username = 'vieeha925'
print(f"--- Checking ScrapedVideo for user: {username} ---")

# Try exact match
videos = ScrapedVideo.objects.filter(author_username=username)
print(f"Found {videos.count()} videos (exact match)")

if videos.count() == 0:
    # Try with @
    videos = ScrapedVideo.objects.filter(author_username=f"@{username}")
    print(f"Found {videos.count()} videos (with @)")

if videos.count() > 0:
    print("\n--- Inspecting first 5 videos ---")
    for v in videos[:5]:
        print(f"ID: {v.video_id}")
        print(f"Title: {v.title[:30]}...")
        
        # Check logic extract duration
        dur = 0
        if isinstance(v.raw_data, dict):
            dur = v.raw_data.get('duration', 0)
            if not dur: dur = v.raw_data.get('video', {}).get('duration', 0)
            if not dur: dur = v.raw_data.get('video_duration', 0)
        
        print(f"Raw Data Duration Key Check: {dur}")
        print(f"Raw Data Keys: {list(v.raw_data.keys())[:5]}")
        print("-" * 20)
else:
    print("NO DATA FOUND! Crawler might not have saved data correctly.")
