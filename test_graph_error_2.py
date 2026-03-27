import os
import requests
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/me/posts"
params = {
    "fields": "id,message,created_time,permalink_url,full_picture,attachments{media,type,url}",
    "limit": 2,
    "access_token": token
}
r = requests.get(url, params=params)
print("STATUS WITHOUT ENGAGEMENT:", r.status_code)

params["fields"] = "id,likes.summary(true)"
r2 = requests.get(url, params=params)
print("STATUS LIKES:", r2.status_code)
print("TEXT LIKES:", r2.text)
