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
}

print("Running Apify facebook-pages-scraper...")
run = client.actor('apify/facebook-pages-scraper').call(run_input=run_input)
if run and run.get('status') == 'SUCCEEDED':
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    print(f"Found {len(items)} items")
    if items:
        # A single item is the page, it has 'posts' property usually
        page_data = items[0]
        posts = page_data.get('posts', [])
        print(f"Found {len(posts)} posts inside page data!")
        for p in posts[:2]:
            print(f"- {p.get('text', '')[:30]!r} | likes: {p.get('likes')} comments: {p.get('comments')}")
