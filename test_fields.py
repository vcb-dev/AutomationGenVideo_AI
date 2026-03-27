import os
import requests
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/me/posts"

print("Trying base fields...")
r1 = requests.get(url, params={"fields": "id,message,created_time,permalink_url", "limit": 1, "access_token": token})
print("R1:", r1.status_code)

print("\nTrying with full_picture...")
r2 = requests.get(url, params={"fields": "id,full_picture", "limit": 1, "access_token": token})
print("R2:", r2.status_code)

print("\nTrying with attachments...")
r3 = requests.get(url, params={"fields": "id,attachments{media}", "limit": 1, "access_token": token})
print("R3:", r3.status_code)

print("\nTrying with picture...")
r4 = requests.get(url, params={"fields": "id,picture", "limit": 1, "access_token": token})
print("R4:", r4.status_code)

print("\nTrying with status_type...")
r5 = requests.get(url, params={"fields": "id,status_type", "limit": 1, "access_token": token})
print("R5:", r5.status_code)
