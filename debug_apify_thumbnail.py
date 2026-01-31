
import os
import sys
import django
import json

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.apify_service import create_scraper

def debug_thumbnail():
    """Debug Apify response to find correct thumbnail field."""
    print("Initializing Apify scraper...")
    scraper = create_scraper('facebook')
    
    # Using the username provided in previous logs
    username = "huyk.kimhoanvienchibao" 
    
    print(f"Fetching top 2 posts for {username} to inspect data structure...")
    try:
        # Fetch 2 posts to increase chance of getting both video and image
        results = scraper.get_user_videos(username, max_results=2)
        
        if results:
            print(f"\n[OK] Fetched {len(results)} items.")
            
            for i, item in enumerate(results):
                print(f"\n{'='*20} ITEM {i+1} {'='*20}")
                
                # Print specific potential image fields to check existence
                print("\n--- POTENTIAL IMAGE FIELDS ---")
                keys_to_check = ['image', 'imageUrl', 'thumbnail', 'fullImage', 'images', 'attachments', 'media']
                for key in keys_to_check:
                    if key in item:
                        val = item[key]
                        if isinstance(val, (list, dict)):
                            print(f"{key}: {type(val).__name__} (len={len(val)} if list)")
                        else:
                            print(f"{key}: {val}")
                    else:
                        print(f"{key}: [MISSING]")

                print("\n--- FULL RAW DATA (Snippet) ---")
                # Print dump but truncate long strings
                dump = json.dumps(item, indent=2, default=str)
                if len(dump) > 2000:
                    print(dump[:2000] + "\n... (truncated)")
                else:
                    print(dump)

                print("\n--- NORMALIZED RESULT ---")
                norm = scraper.normalize_video_data(item)
                print(f"Thumbnail URL: {norm.get('thumbnail_url')}")
                print(f"Is Video: {norm.get('is_video')}")
                
                # Write to file for full inspection
                with open('debug_output.txt', 'w', encoding='utf-8') as f:
                    f.write(json.dumps(results, indent=2, default=str))
                print("\n[OK] Wrote full output to debug_output.txt")
                break # Only process first item

        else:
            print("[FAIL] No items returned from Apify.")
            
    except Exception as e:
        print(f"[ERROR] Error: {e}")

if __name__ == "__main__":
    debug_thumbnail()
