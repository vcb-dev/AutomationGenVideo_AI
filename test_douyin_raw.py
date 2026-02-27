
import os
import json
import logging
from apify_client import ApifyClient
from dotenv import load_dotenv

# Load env from the project directory
load_dotenv('c:/Users/pc/Documents/Github/vienchibao_dev/AutomationGenVideo_AI/.env')

APIFY_API_TOKEN = os.getenv('APIFY_API_TOKEN')
APIFY_ACTOR_DOUYIN = os.getenv('APIFY_ACTOR_DOUYIN', 'natanielsantos/douyin-scraper')

def test_search():
    client = ApifyClient(APIFY_API_TOKEN)
    actor_id = 'natanielsantos/douyin-scraper'
    
    actor_input = {
        "searchTermsOrHashtags": ["美食"],
        "sortBy": "general",
        "publishTime": "all",
        "maxItemsPerUrl": 1, 
        "maxPosts": 1,
        "shouldDownloadCovers": True,
        "shouldDownloadVideos": False,
        "shouldDownloadAuthors": True
    }
    
    print(f"Calling actor {actor_id}...")
    run = client.actor(actor_id).call(run_input=actor_input, timeout_secs=120)
    
    if run['status'] != 'SUCCEEDED':
        print(f"Actor failed with status: {run['status']}")
        return

    dataset_id = run.get('defaultDatasetId')
    print(f"Fetching results from dataset: {dataset_id}")
    
    items = []
    for item in client.dataset(dataset_id).iterate_items():
        items.append(item)
    
    with open('c:/Users/pc/Documents/Github/vienchibao_dev/AutomationGenVideo_AI/douyin_raw_debug.json', 'w', encoding='utf-8') as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    
    print(f"Saved {len(items)} items to douyin_raw_debug.json")

if __name__ == "__main__":
    test_search()
