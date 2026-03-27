import os
import requests
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
from video_management.services.apify_service import ApifyScraperService
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/me/posts"
params = {
    "fields": "id,message,created_time,permalink_url,full_picture,attachments{media,type,url},type",
    "limit": min(2, 100),
    "access_token": token
}
print(f"Calling: {url}")
r = requests.get(url, params=params, timeout=20)
print(r.status_code)
print(r.text)
