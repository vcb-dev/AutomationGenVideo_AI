import os
import requests
import django
import json

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

token = getattr(django.conf.settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/me/posts"

# Testing basic fields
fields = "id,message,created_time,permalink_url,type"
params = {"fields": fields, "limit": 2, "access_token": token}

r = requests.get(url, params=params)
with open('debug_result_v3.json', 'w', encoding='utf-8') as f:
    json.dump({'status': r.status_code, 'body': r.json() if r.status_code == 200 else r.text}, f, indent=2)
