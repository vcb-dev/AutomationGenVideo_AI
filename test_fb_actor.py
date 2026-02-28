"""
Script test: Kiểm tra raw output từ scraper_one/facebook-posts-search
để xem chính xác data format trả về.
"""
import os
import sys
import json
import django

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
from apify_client import ApifyClient

def test_facebook_search():
    token = settings.APIFY_API_TOKEN
    if not token:
        print("ERROR: APIFY_API_TOKEN not set!")
        return
    
    client = ApifyClient(token)
    actor_id = 'scraper_one/facebook-posts-search'
    
    actor_input = {
        "query": "shopee trang sức",
        "resultsCount": 5,
        "searchType": "top",
    }
    
    print(f"Running actor: {actor_id}")
    print(f"Input: {json.dumps(actor_input, ensure_ascii=False)}")
    print("=" * 80)
    
    try:
        run = client.actor(actor_id).call(run_input=actor_input)
        items = list(client.dataset(run['defaultDatasetId']).iterate_items())
        
        print(f"\nGot {len(items)} results")
        print("=" * 80)
        
        for i, item in enumerate(items):
            print(f"\n--- Result {i+1} ---")
            print(f"Keys: {list(item.keys())}")
            print(f"postId: {item.get('postId', 'N/A')}")
            
            url = item.get('url', '')
            print(f"url: {(url or '')[:80]}...")
            
            # Save full raw data for analysis
        with open('test_fb_raw_output.json', 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        print(f"\nFull raw data saved to test_fb_raw_output.json")
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    test_facebook_search()
