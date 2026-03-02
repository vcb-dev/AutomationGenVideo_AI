import os
import sys
import json
import django
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
from apify_client import ApifyClient

def test_fb_posts_scraper():
    token = settings.APIFY_API_TOKEN
    client = ApifyClient(token)
    actor_id = 'apify/facebook-posts-scraper'
    
    actor_input = {
        "startUrls": [
            { "url": "https://www.facebook.com/www.mwc.vn/posts/pfbid02koqoZ5oAKSqxs7Rqmg1Zkh6GbjaJT56REbiAgxFCDvEPoe4qEbEoMdcJd16ZB8XDl" }
        ],
        "resultsLimit": 1,
    }
    
    print(f"Running actor: {actor_id}")
    run = client.actor(actor_id).call(run_input=actor_input)
    items = list(client.dataset(run['defaultDatasetId']).iterate_items())
    
    with open('test_fb_posts_scraper_out.json', 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"\nGot {len(items)} results, saved to test_fb_posts_scraper_out.json")
    
if __name__ == '__main__':
    test_fb_posts_scraper()
