import os
import requests
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/me/posts"
params = {
    "fields": "id,message,created_time,permalink_url,full_picture,attachments{media,type,url},likes.summary(true),comments.summary(true),shares,reactions.summary(true),type",
    "limit": 2,
    "access_token": token
}
r = requests.get(url, params=params)
print("STATUS:", r.status_code)
print("TEXT:", r.text)
