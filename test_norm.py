import os
import sys
import json
import django
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.apify_service import ApifyScraperService
from video_management.models import Platform

logging.basicConfig(level=logging.DEBUG)

def test():
    service = ApifyScraperService(Platform.FACEBOOK)
    
    with open('test_fb_raw_output.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    for i, item in enumerate(data):
        norm = service._normalize_facebook_data(item)
        print(f"\n--- Item {i} ---")
        print(f"ID: {norm.get('video_id')}")
        print(f"URL: {norm.get('video_url')}")
        print(f"Thumb: {norm.get('thumbnail_url')}")
        print(f"Author: {norm.get('author_name')}")

if __name__ == '__main__':
    test()
