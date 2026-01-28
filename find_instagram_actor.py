"""
Search and test Instagram actors on Apify Store.
"""
import os
from apify_client import ApifyClient
import json

APIFY_TOKEN = os.getenv('APIFY_API_TOKEN', 'apify_api_ROfmXCocIdxvpgd69J0BelwUsTMZ8w0i4cGu')
client = ApifyClient(APIFY_TOKEN)

print("=" * 80)
print("SEARCHING FOR INSTAGRAM ACTORS ON APIFY")
print("=" * 80)

# Popular Instagram actors to test
actors_to_test = [
    {
        'id': 'apify/instagram-scraper',
        'name': 'Official Instagram Scraper',
        'input': {
            "directUrls": ["https://www.instagram.com/explore/tags/fashion/"],
            "resultsLimit": 5,
        }
    },
    {
        'id': 'apify/instagram-post-scraper',
        'name': 'Instagram Post Scraper',
        'input': {
            "hashtags": ["fashion"],
            "resultsLimit": 5,
        }
    },
    {
        'id': 'zuzka/instagram-hashtag-scraper',
        'name': 'Zuzka Instagram Hashtag Scraper',
        'input': {
            "hashtags": ["fashion"],
            "resultsLimit": 5,
        }
    },
    {
        'id': 'jaroslavhejlek/instagram-scraper',
        'name': 'Jaroslav Instagram Scraper',
        'input': {
            "search": "#fashion",
            "resultsLimit": 5,
        }
    },
]

successful_actors = []

for idx, actor_info in enumerate(actors_to_test, 1):
    print(f"\n[{idx}/{len(actors_to_test)}] Testing: {actor_info['name']}")
    print(f"Actor ID: {actor_info['id']}")
    print("-" * 80)
    
    try:
        print("Starting actor run...")
        run = client.actor(actor_info['id']).call(
            run_input=actor_info['input'],
            timeout_secs=180
        )
        
        print(f"✓ Run completed with status: {run['status']}")
        
        if run['status'] == 'SUCCEEDED':
            dataset_id = run.get("defaultDatasetId")
            if dataset_id:
                items = list(client.dataset(dataset_id).iterate_items())
                
                print(f"✓ Retrieved {len(items)} items")
                
                if items and len(items) > 0:
                    print(f"\n🎉 SUCCESS! This actor works!")
                    print("\nFirst item structure:")
                    print(json.dumps(items[0], indent=2, ensure_ascii=False)[:500] + "...")
                    
                    print("\nAvailable fields:")
                    print(list(items[0].keys()))
                    
                    successful_actors.append({
                        'id': actor_info['id'],
                        'name': actor_info['name'],
                        'sample_data': items[0]
                    })
                else:
                    print("✗ No items returned (empty dataset)")
            else:
                print("✗ No dataset ID in response")
        else:
            print(f"✗ Run failed with status: {run['status']}")
            
    except Exception as e:
        error_msg = str(e)
        print(f"✗ Error: {error_msg[:200]}")
        if "does not exist" in error_msg or "not found" in error_msg.lower():
            print("  (Actor not found - may not exist)")

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)

if successful_actors:
    print(f"\n✅ Found {len(successful_actors)} working actor(s):\n")
    for actor in successful_actors:
        print(f"  • {actor['name']}")
        print(f"    ID: {actor['id']}")
        print()
    
    print("\nRecommended actor to use:")
    best = successful_actors[0]
    print(f"  {best['id']}")
    
    print("\nSample field mapping:")
    sample = best['sample_data']
    print(f"  id: {sample.get('id')}")
    print(f"  caption: {sample.get('caption', sample.get('text', ''))[:50]}...")
    print(f"  likes: {sample.get('likesCount', sample.get('likes'))}")
    print(f"  comments: {sample.get('commentsCount', sample.get('comments'))}")
    
else:
    print("\n❌ No working Instagram actors found on free tier")
    print("\nPossible solutions:")
    print("  1. Upgrade Apify plan for better Instagram access")
    print("  2. Use TikTok/Douyin as main source (working well)")
    print("  3. Use mock data for Instagram demo")
    print("  4. Implement direct Instagram API (requires auth)")

print("\n" + "=" * 80)
