import os
import requests
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.apify_service import ApifyScraperService
from video_management.models import Platform

scraper = ApifyScraperService(Platform.FACEBOOK)
print("Testing Page Info Hook...")
info = scraper.get_page_info('61580182263005')
print("INFO:", info)

print("\nTesting Posts Hook...")
posts = scraper.get_user_videos('61580182263005', max_results=2)
print("POSTS FOUND:", len(posts))
for p in posts:
    print(f"- id={p.get('postId')} text={p.get('text')[:30]!r} videoUrl={p.get('videoUrl')}")
