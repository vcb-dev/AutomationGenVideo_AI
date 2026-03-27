import os
import requests
import time
from django.conf import settings
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

import apify_client
client = apify_client.ApifyClient(settings.APIFY_API_TOKEN)

run_input = {
    "startUrls": [{"url": "https://www.facebook.com/763339010200340"}],
    "resultsLimit": 10,
    "maxPostCount": 10,
    "searchMode": "posts",
    "proxyConfiguration": {
        "useApifyProxy": True,
        "apifyProxyGroups": ["RESIDENTIAL"],
        "apifyProxyCountry": "VN"
    }
}

print("Running Apify...")
run = client.actor('apify/facebook-posts-scraper').call(run_input=run_input)
if run and run.get('status') == 'SUCCEEDED':
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    print(f"Found {len(items)} items")
    for item in items[:2]:
        print(item.get("url"), item.get("error"))
