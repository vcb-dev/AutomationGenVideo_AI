"""
Test script to verify Facebook data from Apify.

This script tests whether Apify returns:
1. Number of posts (POSTS count)
2. Number of followers (FOLLOWERS count)
3. Other channel metadata

Run: python test_facebook_apify_data.py
"""

import os
import sys
import json
import logging
from typing import Dict, Any, List
from datetime import datetime

# Django setup
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from apify_client import ApifyClient
from django.conf import settings
from video_management.models import Platform

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FacebookApifyTester:
    """Test Facebook data retrieval from Apify."""
    
    def __init__(self):
        """Initialize Apify client."""
        self.api_token = getattr(settings, 'APIFY_API_TOKEN', '')
        if not self.api_token:
            raise Exception("APIFY_API_TOKEN not configured in settings")
        
        self.client = ApifyClient(self.api_token)
        
        # Get Facebook actor ID
        actors = getattr(settings, 'APIFY_ACTORS', {})
        self.actor_id = actors.get('facebook', '')
        
        if not self.actor_id:
            raise Exception("No Facebook actor configured in APIFY_ACTORS")
        
        logger.info(f"✅ Initialized with actor: {self.actor_id}")
    
    def test_page_profile(self, page_username: str) -> Dict[str, Any]:
        """
        Test fetching Facebook page/profile data.
        
        Args:
            page_username: Facebook page username (e.g., 'HuyKKimHoan' or full URL)
        
        Returns:
            Test results with all available data
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"🔍 Testing Facebook Page: {page_username}")
        logger.info(f"{'='*60}\n")
        
        # Clean username
        clean_username = page_username.replace('@', '').strip()
        if 'facebook.com/' in clean_username:
            # Extract username from URL
            clean_username = clean_username.split('facebook.com/')[-1].split('/')[0]
        
        # Build actor input - try multiple configurations
        test_configs = [
            {
                "name": "Profile Scraper (startUrls)",
                "input": {
                    "startUrls": [{"url": f"https://www.facebook.com/{clean_username}"}],
                    "resultsLimit": 10,
                }
            },
            {
                "name": "Profile Scraper (with profile fields)",
                "input": {
                    "startUrls": [{"url": f"https://www.facebook.com/{clean_username}"}],
                    "resultsLimit": 10,
                    "scrapeAbout": True,  # Try to get profile info
                    "scrapeReviews": False,
                    "scrapePosts": True,
                }
            }
        ]
        
        results = {
            "page_username": clean_username,
            "test_time": datetime.now().isoformat(),
            "actor_id": self.actor_id,
            "tests": []
        }
        
        for config in test_configs:
            logger.info(f"\n📋 Testing config: {config['name']}")
            logger.info(f"Input: {json.dumps(config['input'], indent=2)}")
            
            try:
                # Run actor
                logger.info("⏳ Running actor...")
                run = self.client.actor(self.actor_id).call(
                    run_input=config['input'],
                    timeout_secs=120
                )
                
                # Check status
                status = run.get('status')
                logger.info(f"Status: {status}")
                
                if status != 'SUCCEEDED':
                    logger.error(f"❌ Actor failed with status: {status}")
                    results['tests'].append({
                        "config": config['name'],
                        "status": "FAILED",
                        "error": f"Actor status: {status}"
                    })
                    continue
                
                # Get dataset
                dataset_id = run.get('defaultDatasetId')
                logger.info(f"Dataset ID: {dataset_id}")
                
                # Fetch items
                items = []
                for item in self.client.dataset(dataset_id).iterate_items():
                    items.append(item)
                
                logger.info(f"✅ Retrieved {len(items)} items")
                
                # Analyze data
                test_result = self._analyze_data(items, config['name'])
                results['tests'].append(test_result)
                
            except Exception as e:
                logger.error(f"❌ Error: {str(e)}", exc_info=True)
                results['tests'].append({
                    "config": config['name'],
                    "status": "ERROR",
                    "error": str(e)
                })
        
        return results
    
    def _analyze_data(self, items: List[Dict[str, Any]], config_name: str) -> Dict[str, Any]:
        """Analyze retrieved data for posts count and followers."""
        logger.info(f"\n{'='*60}")
        logger.info("📊 DATA ANALYSIS")
        logger.info(f"{'='*60}\n")
        
        result = {
            "config": config_name,
            "status": "SUCCESS",
            "total_items": len(items),
            "posts_count": 0,
            "followers_count": 0,
            "likes_count": 0,
            "found_fields": set(),
            "sample_item": None
        }
        
        if not items:
            logger.warning("⚠️  No items returned")
            return result
        
        # Analyze first item (usually profile/page info)
        first_item = items[0]
        result['sample_item'] = first_item
        
        logger.info(f"📄 First item keys: {list(first_item.keys())}")
        logger.info(f"\n📄 First item (full):\n{json.dumps(first_item, indent=2, default=str)}\n")
        
        # Check for common fields
        field_mappings = {
            # Posts count variations
            'posts_count': ['postsCount', 'posts', 'postCount', 'totalPosts', 'videoCount', 'videos'],
            # Followers variations
            'followers_count': ['followersCount', 'followers', 'likes', 'pageFollowers', 'pageLikes', 'fanCount'],
            # Likes variations
            'likes_count': ['likesCount', 'likes', 'pageLikes', 'totalLikes']
        }
        
        logger.info("\n🔍 Searching for key fields:")
        
        for result_key, possible_fields in field_mappings.items():
            for field in possible_fields:
                if field in first_item:
                    value = first_item[field]
                    result[result_key] = value
                    result['found_fields'].add(field)
                    logger.info(f"  ✅ Found '{field}': {value}")
                    break
        
        # Count actual posts in items
        actual_posts = len([item for item in items if item.get('postId') or item.get('id')])
        if actual_posts > 0:
            logger.info(f"  ✅ Actual posts in dataset: {actual_posts}")
            result['posts_count'] = max(result['posts_count'], actual_posts)
        
        # Summary
        logger.info(f"\n{'='*60}")
        logger.info("📈 SUMMARY")
        logger.info(f"{'='*60}")
        logger.info(f"  Posts Count: {result['posts_count']}")
        logger.info(f"  Followers Count: {result['followers_count']}")
        logger.info(f"  Likes Count: {result['likes_count']}")
        logger.info(f"  Found Fields: {', '.join(result['found_fields']) if result['found_fields'] else 'None'}")
        
        # Convert set to list for JSON serialization
        result['found_fields'] = list(result['found_fields'])
        
        return result
    
    def save_results(self, results: Dict[str, Any], filename: str = "facebook_apify_test_results.json"):
        """Save test results to file."""
        filepath = os.path.join(os.path.dirname(__file__), filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"\n💾 Results saved to: {filepath}")


def main():
    """Main test function."""
    # Test with the username from the screenshot
    test_username = "100063721444581"  # From screenshot: @100063721444581
    
    # Alternative: You can also test with a page name
    # test_username = "HuyKKimHoan"
    
    try:
        tester = FacebookApifyTester()
        results = tester.test_page_profile(test_username)
        tester.save_results(results)
        
        # Print final verdict
        logger.info(f"\n{'='*60}")
        logger.info("🎯 FINAL VERDICT")
        logger.info(f"{'='*60}")
        
        has_posts = any(test.get('posts_count', 0) > 0 for test in results['tests'])
        has_followers = any(test.get('followers_count', 0) > 0 for test in results['tests'])
        
        if has_posts:
            logger.info("✅ Posts count: FOUND")
        else:
            logger.error("❌ Posts count: NOT FOUND (returns 0)")
        
        if has_followers:
            logger.info("✅ Followers count: FOUND")
        else:
            logger.error("❌ Followers count: NOT FOUND (returns 0)")
        
        if not has_posts or not has_followers:
            logger.warning("\n⚠️  RECOMMENDATION:")
            logger.warning("  - Check if the Apify actor supports profile metadata")
            logger.warning("  - Consider using Facebook Graph API for accurate stats")
            logger.warning("  - Or use a different Apify actor (e.g., 'apify/facebook-pages-scraper')")
        
    except Exception as e:
        logger.error(f"❌ Test failed: {str(e)}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
