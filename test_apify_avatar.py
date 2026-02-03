"""
Test script to check what avatar field Apify Instagram Profile Scraper returns
"""
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'automation_gen_video.settings')
import django
django.setup()

from apify_client import ApifyClient
from django.conf import settings
import json

def test_instagram_profile_avatar():
    """Test what avatar field Apify returns for Instagram profile"""
    
    # Initialize Apify client
    api_token = settings.APIFY_API_TOKEN
    client = ApifyClient(api_token)
    
    # Test username
    username = "huyk_mekimhoan"
    
    print(f"\n{'='*80}")
    print(f"Testing Apify Instagram Profile Scraper for: @{username}")
    print(f"{'='*80}\n")
    
    # Run the actor
    actor_input = {
        "usernames": [username],
        "resultsLimit": 1  # Only need profile info
    }
    
    print("🚀 Calling Apify actor: apify/instagram-profile-scraper")
    print(f"   Input: {json.dumps(actor_input, indent=2)}\n")
    
    run = client.actor("apify/instagram-profile-scraper").call(
        run_input=actor_input,
        timeout_secs=60
    )
    
    print(f"✅ Actor run completed with status: {run['status']}\n")
    
    # Get results
    dataset_id = run.get('defaultDatasetId')
    items = list(client.dataset(dataset_id).iterate_items())
    
    if not items:
        print("❌ No data returned!")
        return
    
    profile_data = items[0]
    
    # Print ALL keys
    print(f"📋 ALL KEYS IN RESPONSE ({len(profile_data.keys())} total):")
    print(f"   {list(profile_data.keys())}\n")
    
    # Print avatar-related fields
    print("🖼️  AVATAR-RELATED FIELDS:")
    avatar_keys = [k for k in profile_data.keys() if any(x in k.lower() for x in ['pic', 'avatar', 'image', 'photo'])]
    
    if avatar_keys:
        for key in avatar_keys:
            value = profile_data.get(key)
            if isinstance(value, str) and len(value) > 100:
                print(f"   {key}: {value[:100]}...")
            else:
                print(f"   {key}: {value}")
    else:
        print("   ⚠️  NO avatar-related fields found!")
    
    # Print basic profile info
    print(f"\n👤 PROFILE INFO:")
    print(f"   Username: {profile_data.get('username')}")
    print(f"   Full Name: {profile_data.get('fullName')}")
    print(f"   Followers: {profile_data.get('followersCount', 0):,}")
    print(f"   Posts: {profile_data.get('postsCount', 0):,}")
    
    # Save full response to file for inspection
    output_file = project_root / "apify_profile_response.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(profile_data, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 Full response saved to: {output_file}")
    print(f"\n{'='*80}\n")

if __name__ == "__main__":
    test_instagram_profile_avatar()
