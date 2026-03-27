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
    "fields": "id,message,likes.summary(true),comments.summary(true),shares,views",
    "limit": 3,
    "access_token": token
}
r = requests.get(url, params=params)
print(json.dumps(r.json(), indent=2))
