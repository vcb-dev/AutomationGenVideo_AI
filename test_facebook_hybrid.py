"""
Test Facebook Hybrid Service.

This script demonstrates the hybrid approach:
- Pages/Groups → Graph API (accurate followers + posts count)
- User Profiles → Apify (posts list only)
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

from video_management.services.facebook_hybrid_service import get_facebook_hybrid_service

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_url(url: str, force_method: str = None):
    """Test a Facebook URL."""
    logger.info("\n" + "="*70)
    logger.info(f"🔗 Testing URL: {url}")
    if force_method:
        logger.info(f"🔧 Forced method: {force_method}")
    logger.info("="*70 + "\n")
    
    try:
        service = get_facebook_hybrid_service()
        result = service.get_facebook_data(url, max_posts=10, force_method=force_method)
        
        # Display results
        logger.info("\n📊 RESULTS:")
        logger.info("="*70)
        logger.info(f"  Type: {result['type'].upper()}")
        logger.info(f"  Method: {result['method'].upper()}")
        logger.info(f"  Name: {result['name']}")
        logger.info(f"  Identifier: {result['identifier']}")
        
        if result['followers_count'] is not None:
            logger.info(f"  Followers: {result['followers_count']:,}")
        else:
            logger.info(f"  Followers: N/A (not available for {result['type']})")
        
        if result['posts_count'] is not None:
            if result['method'] == 'apify':
                logger.info(f"  Posts: {result['posts_count']} (fetched, not total)")
            else:
                logger.info(f"  Posts: {result['posts_count']:,} (total)")
        else:
            logger.info(f"  Posts: N/A")
        
        logger.info("="*70)
        
        # Save results
        filename = f"facebook_hybrid_test_{result['type']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"\n💾 Results saved to: {filename}")
        
        return result
        
    except Exception as e:
        logger.error(f"\n❌ Test failed: {str(e)}", exc_info=True)
        return None


def main():
    """Main test function."""
    logger.info("\n" + "="*70)
    logger.info("🚀 FACEBOOK HYBRID SERVICE TEST")
    logger.info("="*70)
    logger.info(f"Test Time: {datetime.now().isoformat()}")
    logger.info("="*70 + "\n")
    
    # Check available methods
    service = get_facebook_hybrid_service()
    methods = service.get_available_methods()
    
    logger.info("📋 Available Methods:")
    logger.info(f"  - Graph API: {'✅ Available' if methods['graph_api'] else '❌ Not configured'}")
    logger.info(f"  - Apify: {'✅ Available' if methods['apify'] else '❌ Not configured'}")
    logger.info("")
    
    # Test cases
    test_cases = [
        {
            'name': 'Facebook Page (should use Graph API)',
            'url': 'https://www.facebook.com/facebook',
            'force_method': None
        },
        {
            'name': 'User Profile (should use Apify)',
            'url': 'https://www.facebook.com/100063721444581',
            'force_method': None
        },
        {
            'name': 'User Profile (forced Graph API - will fail)',
            'url': 'https://www.facebook.com/100063721444581',
            'force_method': 'graph'
        },
        {
            'name': 'Facebook Page (forced Apify)',
            'url': 'https://www.facebook.com/facebook',
            'force_method': 'apify'
        },
    ]
    
    results = []
    
    for i, test_case in enumerate(test_cases, 1):
        logger.info(f"\n{'='*70}")
        logger.info(f"TEST {i}/{len(test_cases)}: {test_case['name']}")
        logger.info(f"{'='*70}")
        
        result = test_url(test_case['url'], test_case.get('force_method'))
        results.append({
            'test_name': test_case['name'],
            'success': result is not None,
            'result': result
        })
        
        # Wait a bit between tests
        import time
        time.sleep(2)
    
    # Final summary
    logger.info("\n" + "="*70)
    logger.info("📈 FINAL SUMMARY")
    logger.info("="*70)
    
    passed = sum(1 for r in results if r['success'])
    total = len(results)
    
    logger.info(f"  Tests Passed: {passed}/{total}")
    logger.info(f"  Success Rate: {(passed/total)*100:.0f}%")
    
    logger.info("\n📊 Test Results:")
    for i, result in enumerate(results, 1):
        status = "✅ PASS" if result['success'] else "❌ FAIL"
        logger.info(f"  {i}. {status} - {result['test_name']}")
    
    if passed == total:
        logger.info("\n🎉 ALL TESTS PASSED!")
    else:
        logger.warning(f"\n⚠️  {total - passed} test(s) failed")
    
    logger.info("="*70 + "\n")


if __name__ == "__main__":
    main()
