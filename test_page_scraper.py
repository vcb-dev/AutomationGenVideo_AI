
import os
import django
import sys
import json
from apify_client import ApifyClient

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings

def test_page_scrape():
    token = settings.APIFY_API_TOKEN
    client = ApifyClient(token)
    
    # "HuyK - Mê kim hoàn" -> huyk.kimhoanvienchibao (from screenshot)
    # Or try the ID from previous logs: 100063721444581
    # Let's try the username derived from user context or screenshot.
    # Screenshot shows "@61559648395783" (?) No that's not it. 
    # Ah, the screenshot 2 shows "HuyK - Mê kim hoàn" 
    # Let's use the startUrl logic.

    print("Testing Facebook Page Scraper...")
    
    # Using the actor configured in settings
    actor_id = getattr(settings, 'APIFY_ACTORS', {}).get('facebook_page', 'apify/facebook-pages-scraper')
    print(f"Actor: {actor_id}")

    run_input = {
        "startUrls": [{"url": "https://www.facebook.com/huyk.kimhoanvienchibao"}],
        "maxItems": 1
    }
    
    print(f"Input: {run_input}")
    
    # Run
    run = client.actor(actor_id).call(run_input=run_input, timeout_secs=120)
    
    if run['status'] == 'SUCCEEDED':
        dataset_id = run.get('defaultDatasetId')
        items = list(client.dataset(dataset_id).iterate_items())
        if items:
            item = items[0]
            print("\n--- RESULTS ---")
            print(json.dumps(item, indent=2))
            
            with open('facebook_page_raw.json', 'w', encoding='utf-8') as f:
                json.dump(item, f, indent=2, ensure_ascii=False)
                
            print(f"\nSaved raw data to facebook_page_raw.json")
        else:
            print("No items returned.")
    else:
        print(f"Run Error: {run}")

if __name__ == "__main__":
    test_page_scrape()
