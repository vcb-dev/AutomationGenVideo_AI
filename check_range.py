import os
import requests
import django
from datetime import datetime, timezone
import dateutil.parser

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/me/posts"
params = {
    "fields": "id,created_time,shares,comments.summary(true)",
    "limit": 100,
    "access_token": token
}
r = requests.get(url, params=params)
data = r.json().get('data', [])

start = dateutil.parser.isoparse("2026-03-24T00:00:00+00:00")
end = dateutil.parser.isoparse("2026-03-27T23:59:59+00:00")

filtered = [p for p in data if start <= dateutil.parser.isoparse(p['created_time']) <= end]

total_comments = sum(p.get('comments', {}).get('summary', {}).get('total_count', 0) for p in filtered)
total_shares = sum(p.get('shares', {}).get('count', 0) for p in filtered)

print(f"Total Comments in range: {total_comments}")
print(f"Total Shares in range: {total_shares}")
