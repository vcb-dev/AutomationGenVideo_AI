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

r = requests.get(url, params=params, timeout=10)
print(f"DEBUG_STATUS: {r.status_code}")
if r.status_code != 200:
    print(f"DEBUG_ERROR: {r.text}")
else:
    print("DEBUG_SUCCESS")
