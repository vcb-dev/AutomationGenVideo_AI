"""
Quick script to check Django database videos.
"""
import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import ScrapedVideo

# Query videos
print("=" * 80)
print("📊 DJANGO DATABASE - SCRAPED VIDEOS")
print("=" * 80)

# Total count
total = ScrapedVideo.objects.count()
print(f"\n✅ Total videos in database: {total}")

# By platform
print("\n📱 Videos by platform:")
platforms = ScrapedVideo.objects.values('platform').distinct()
for p in platforms:
    count = ScrapedVideo.objects.filter(platform=p['platform']).count()
    print(f"  - {p['platform']}: {count} videos")

# Recent videos
print("\n🎬 Recent 5 videos:")
recent = ScrapedVideo.objects.order_by('-created_at')[:5]
for v in recent:
    print(f"  - [{v.platform}] @{v.author_username}: {v.title[:50]}...")

# Check specific channel
username = "huyk.trangsucchetac"
print(f"\n🔍 Searching for username containing '{username}':")
matching = ScrapedVideo.objects.filter(author_username__icontains=username)
print(f"  Found: {matching.count()} videos")

if matching.exists():
    print("\n  Sample videos:")
    for v in matching[:3]:
        print(f"    - {v.video_id}: {v.title[:40]}... (👁️ {v.views_count}, ❤️ {v.likes_count})")
        print(f"      Author: @{v.author_username} (Platform: {v.platform})")

# All unique usernames (first 10)
print("\n👥 Sample usernames in database:")
usernames = ScrapedVideo.objects.values_list('author_username', flat=True).distinct()[:10]
for u in usernames:
    print(f"  - @{u}")

print("\n" + "=" * 80)
