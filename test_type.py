import os
import requests
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/me/posts"

print("Trying with type...")
r6 = requests.get(url, params={"fields": "id,type", "limit": 1, "access_token": token})
print("R6:", r6.status_code)
print(r6.text)
