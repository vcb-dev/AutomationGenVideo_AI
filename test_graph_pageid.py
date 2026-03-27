import os
import requests
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
page_id = "763339010200340"
url = f"https://graph.facebook.com/v20.0/{page_id}/posts"

# All fields we want
fields = "id,message,created_time,permalink_url,full_picture,attachments{media,type,url},likes.summary(true),comments.summary(true),shares,reactions.summary(true),type"
params = {
    "fields": fields,
    "limit": 2,
    "access_token": token
}

print(f"Calling: {url}")
r = requests.get(url, params=params, timeout=10)
print(f"STATUS_: {r.status_code}")
if r.status_code != 200:
    print(f"ERROR_: {r.text}")
else:
    print(f"SUCCESS! {list(r.json().get('data', [])[0].keys())}")
