#!/usr/bin/env python
"""
Test script to check scraper timeout and logs
Run this to test the scraper directly and see detailed logs
"""
import asyncio
import sys
import os
import django
import logging

# Setup Django
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from video_management.services.scraper_service import DouyinScraper

async def test_scraper():
    """Test scraper with timeout monitoring"""
    print("=" * 60)
    print("Testing Douyin Scraper")
    print("=" * 60)
    
    keyword = "猫"
    min_likes = 1000
    min_views = 10000
    target_count = 5
    
    print(f"\nParameters:")
    print(f"  Keyword: {keyword}")
    print(f"  Min Likes: {min_likes}")
    print(f"  Min Views: {min_views}")
    print(f"  Target Count: {target_count}")
    print(f"\nStarting scraper (this may take a while)...\n")
    
    scraper = DouyinScraper(headless=False)  # Set to False to see browser
    
    try:
        # Test with timeout
        results = await asyncio.wait_for(
            scraper.search_videos(
                keyword=keyword,
                min_likes=min_likes,
                min_views=min_views,
                target_count=target_count
            ),
            timeout=300.0  # 5 minutes
        )
        
        print("\n" + "=" * 60)
        print(f"Results: Found {len(results)} videos")
        print("=" * 60)
        
        if results:
            for i, video in enumerate(results[:5], 1):
                print(f"\nVideo {i}:")
                print(f"  ID: {video.get('id')}")
                print(f"  Caption: {video.get('caption', 'N/A')[:50]}...")
                print(f"  Likes: {video.get('likes', 0):,}")
                print(f"  Views: {video.get('views', 0):,}")
                print(f"  Channel: {video.get('channelName', 'N/A')}")
                print(f"  URL: {video.get('url', 'N/A')}")
        else:
            print("\nNo videos found. Possible reasons:")
            print("  1. Douyin HTML structure changed")
            print("  2. Selectors are not matching")
            print("  3. Filters are too strict")
            print("  4. Network/timeout issues")
            
    except asyncio.TimeoutError:
        print("\n" + "=" * 60)
        print("ERROR: Scraper timed out after 5 minutes")
        print("=" * 60)
        print("\nPossible solutions:")
        print("  1. Increase timeout in test_scraper_timeout.py")
        print("  2. Check network connection")
        print("  3. Try with different keyword")
        print("  4. Check if Douyin is accessible")
        
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"ERROR: {type(e).__name__}: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_scraper())
