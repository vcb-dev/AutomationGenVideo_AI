import os
import requests
import django
import json

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/me/posts"
params = {
    "fields": "id,shares,comments.summary(true)",
    "limit": 100,
    "access_token": token
}
r = requests.get(url, params=params)
data = r.json().get('data', [])
total_comments = sum(p.get('comments', {}).get('summary', {}).get('total_count', 0) for p in data)
total_shares = sum(p.get('shares', {}).get('count', 0) for p in data)
print(f"Total Comments on 100 posts: {total_comments}")
print(f"Total Shares on 100 posts: {total_shares}")
