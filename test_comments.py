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
    "fields": "id,message,created_time,comments.summary(true).filter(stream),attachments{media,target}",
    "limit": 15,
    "access_token": token
}
r = requests.get(url, params=params)
data = r.json().get('data', [])

found_comments = 0
for p in data:
    count = p.get('comments', {}).get('summary', {}).get('total_count', 0)
    print(f"Post {p['id']} - {p.get('created_time')} - Comments: {count}")
    if count > 0:
        found_comments += count
print(f"Total found stream comments: {found_comments}")
