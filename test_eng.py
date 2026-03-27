import os
import requests
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/me/posts"

print("1. Testing likes connection:")
r1 = requests.get(url, params={"fields": "id,likes{id}", "limit": 1, "access_token": token})
print(f"R1: {r1.status_code}")
if r1.status_code != 200:
    print(f"R1 Error: {r1.text}")
else:
    print(r1.json())

print("\n2. Testing comments connection:")
r2 = requests.get(url, params={"fields": "id,comments{id}", "limit": 1, "access_token": token})
print(f"R2: {r2.status_code}")
if r2.status_code != 200:
    print(f"R2 Error: {r2.text}")
else:
    print(r2.json())
