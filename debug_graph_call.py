import os
import requests
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/me/posts"

# Exactly the fields in apify_service.py
fields = "id,message,created_time,permalink_url,full_picture,attachments{media,type,url},type"
params = {
    "fields": fields,
    "limit": 2,
    "access_token": token
}

print(f"Testing URL: {url}")
print(f"Token (First 10): {token[:10]}...")
r = requests.get(url, params=params, timeout=10)
print(f"Status Code: {r.status_code}")
if r.status_code != 200:
    print(f"Error Body: {r.text}")
else:
    print(f"Success! Data keys: {list(r.json().keys())}")
    print(f"Items found: {len(r.json().get('data', []))}")
