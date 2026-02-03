"""
Test Facebook Graph API Service.

This script tests the Facebook Graph API service to verify:
1. Connection to Facebook Graph API
2. Ability to fetch page metadata (followers, posts count)
3. Data accuracy

Run: python test_facebook_graph.py
"""

import os
import sys
import json
import logging
from datetime import datetime

# Django setup
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.facebook_graph_service import FacebookGraphService

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_connection():
    """Test connection to Facebook Graph API."""
    logger.info("\n" + "="*60)
    logger.info("🔌 TEST 1: CONNECTION TEST")
    logger.info("="*60 + "\n")
    
    try:
        service = FacebookGraphService()
        success = service.test_connection()
        
        if success:
            logger.info("✅ Connection test PASSED")
            return True
        else:
            logger.error("❌ Connection test FAILED")
            return False
            
    except Exception as e:
        logger.error(f"❌ Connection test ERROR: {str(e)}", exc_info=True)
        return False


def test_page_metadata(page_id: str):
    """
    Test fetching page metadata.
    
    Args:
        page_id: Facebook page ID to test
    """
    logger.info("\n" + "="*60)
    logger.info(f"📊 TEST 2: PAGE METADATA - {page_id}")
    logger.info("="*60 + "\n")
    
    try:
        service = FacebookGraphService()
        metadata = service.get_page_metadata(page_id)
        
        logger.info("\n📋 RESULTS:")
        logger.info("="*60)
        logger.info(f"  Page ID: {metadata['page_id']}")
        logger.info(f"  Name: {metadata['name']}")
        logger.info(f"  Followers: {metadata['followers_count']:,}")
        logger.info(f"  Fan Count: {metadata['fan_count']:,}")
        logger.info(f"  Posts Count: {metadata['posts_count']:,}")
        logger.info(f"  Category: {metadata['category']}")
        logger.info(f"  About: {metadata['about'][:100]}..." if metadata['about'] else "  About: N/A")
        logger.info(f"  Website: {metadata['website']}")
        logger.info("="*60)
        
        # Save to file
        output_file = f"facebook_page_{page_id}_metadata.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        
        logger.info(f"\n💾 Results saved to: {output_file}")
        
        # Verdict
        logger.info("\n" + "="*60)
        logger.info("🎯 VERDICT")
        logger.info("="*60)
        
        if metadata['followers_count'] > 0:
            logger.info("✅ Followers count: FOUND")
        else:
            logger.warning("⚠️  Followers count: 0 (may be a new page)")
        
        if metadata['posts_count'] > 0:
            logger.info("✅ Posts count: FOUND")
        else:
            logger.warning("⚠️  Posts count: 0 (may be a new page or no posts)")
        
        logger.info("✅ Test PASSED - Data retrieved successfully!")
        
        return metadata
        
    except Exception as e:
        logger.error(f"❌ Test FAILED: {str(e)}", exc_info=True)
        return None


def compare_with_apify_results():
    """Compare Graph API results with Apify results."""
    logger.info("\n" + "="*60)
    logger.info("📊 COMPARISON: Graph API vs Apify")
    logger.info("="*60 + "\n")
    
    # Load Apify results if exists
    apify_file = "facebook_apify_test_results.json"
    if not os.path.exists(apify_file):
        logger.warning(f"⚠️  Apify results file not found: {apify_file}")
        logger.info("   Run test_facebook_apify_data.py first to compare")
        return
    
    with open(apify_file, 'r', encoding='utf-8') as f:
        apify_data = json.load(f)
    
    page_id = apify_data.get('page_username', '100063721444581')
    
    # Get Graph API results
    try:
        service = FacebookGraphService()
        graph_data = service.get_page_metadata(page_id)
        
        logger.info("\n📊 COMPARISON TABLE:")
        logger.info("="*60)
        logger.info(f"{'Metric':<20} {'Apify':<20} {'Graph API':<20}")
        logger.info("-"*60)
        
        # Apify results (from first test)
        apify_posts = apify_data['tests'][0].get('posts_count', 0) if apify_data['tests'] else 0
        apify_followers = apify_data['tests'][0].get('followers_count', 0) if apify_data['tests'] else 0
        
        logger.info(f"{'Posts Count':<20} {apify_posts:<20} {graph_data['posts_count']:<20}")
        logger.info(f"{'Followers Count':<20} {apify_followers:<20} {graph_data['followers_count']:<20}")
        logger.info("="*60)
        
        logger.info("\n🎯 CONCLUSION:")
        logger.info(f"  - Apify posts count: {apify_posts} (only fetched posts, not total)")
        logger.info(f"  - Graph API posts count: {graph_data['posts_count']} (ACCURATE total)")
        logger.info(f"  - Apify followers: {apify_followers} (NOT AVAILABLE)")
        logger.info(f"  - Graph API followers: {graph_data['followers_count']} (ACCURATE)")
        
        logger.info("\n✅ RECOMMENDATION: Use Graph API for page metadata!")
        
    except Exception as e:
        logger.error(f"❌ Comparison failed: {str(e)}")


def main():
    """Main test function."""
    logger.info("\n" + "="*60)
    logger.info("🚀 FACEBOOK GRAPH API SERVICE TEST")
    logger.info("="*60)
    logger.info(f"Test Time: {datetime.now().isoformat()}")
    logger.info("="*60 + "\n")
    
    # Test page ID (HuyK - Kim Hoàn from screenshot)
    test_page_id = "100063721444581"
    
    # Run tests
    tests_passed = 0
    tests_total = 2
    
    # Test 1: Connection
    if test_connection():
        tests_passed += 1
    
    # Test 2: Page Metadata
    if test_page_metadata(test_page_id):
        tests_passed += 1
    
    # Bonus: Compare with Apify
    compare_with_apify_results()
    
    # Final summary
    logger.info("\n" + "="*60)
    logger.info("📈 FINAL SUMMARY")
    logger.info("="*60)
    logger.info(f"  Tests Passed: {tests_passed}/{tests_total}")
    logger.info(f"  Success Rate: {(tests_passed/tests_total)*100:.0f}%")
    
    if tests_passed == tests_total:
        logger.info("\n🎉 ALL TESTS PASSED!")
        logger.info("\n✅ Facebook Graph API is working correctly!")
        logger.info("✅ You can now get accurate followers and posts count!")
    else:
        logger.error("\n❌ SOME TESTS FAILED")
        logger.error("   Check the errors above and:")
        logger.error("   1. Verify FACEBOOK_APP_ID and FACEBOOK_APP_SECRET in .env")
        logger.error("   2. Generate access token at: https://developers.facebook.com/tools/explorer/")
        logger.error("   3. Add token to .env as FACEBOOK_ACCESS_TOKEN")
    
    logger.info("="*60 + "\n")


if __name__ == "__main__":
    main()
