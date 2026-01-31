"""
Test Facebook Hybrid Service - Updated Version
Tests the new structure with videos/images separation
"""

import os
import sys
import django
import logging
from datetime import datetime

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'video_automation.settings')
django.setup()

from video_management.services.facebook_hybrid_service import get_facebook_hybrid_service

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_facebook_analysis(url: str):
    """Test Facebook analysis with new structure."""
    logger.info("\n" + "="*80)
    logger.info(f"🧪 TESTING FACEBOOK ANALYSIS")
    logger.info("="*80)
    logger.info(f"URL: {url}")
    logger.info(f"Time: {datetime.now().isoformat()}")
    logger.info("="*80 + "\n")
    
    try:
        # Get service
        service = get_facebook_hybrid_service()
        
        # Check available methods
        methods = service.get_available_methods()
        logger.info(f"📋 Available methods:")
        logger.info(f"  - Graph API: {'✅' if methods['graph_api'] else '❌'}")
        logger.info(f"  - Apify: {'✅' if methods['apify'] else '❌'}")
        logger.info("")
        
        # Analyze
        logger.info(f"🚀 Starting analysis...")
        result = service.get_facebook_data(
            url=url,
            max_posts=10,
            force_method='apify'  # Force Apify
        )
        
        # Display results
        logger.info("\n" + "="*80)
        logger.info("✅ ANALYSIS COMPLETE")
        logger.info("="*80)
        logger.info(f"Type: {result['type']}")
        logger.info(f"Method: {result['method']}")
        logger.info(f"Name: {result['name']}")
        logger.info(f"Identifier: {result['identifier']}")
        logger.info(f"Followers: {result['followers_count'] or 'N/A'}")
        logger.info(f"Posts: {result['posts_count']}")
        logger.info("")
        
        # Videos & Images
        videos = result.get('videos', [])
        images = result.get('images', [])
        logger.info(f"📊 Content Breakdown:")
        logger.info(f"  - Videos: {len(videos)}")
        logger.info(f"  - Images: {len(images)}")
        logger.info(f"  - Total Posts: {len(result['posts'])}")
        logger.info("")
        
        # Metadata
        metadata = result.get('metadata', {})
        logger.info(f"📝 Metadata:")
        logger.info(f"  - Profile Pic: {metadata.get('user_profile_pic', 'N/A')[:50]}...")
        logger.info(f"  - Profile URL: {metadata.get('user_profile_url', 'N/A')}")
        logger.info(f"  - Note: {metadata.get('note', 'N/A')}")
        logger.info("")
        
        # Sample posts
        if result['posts']:
            logger.info(f"📄 Sample Posts (first 3):")
            for i, post in enumerate(result['posts'][:3], 1):
                logger.info(f"\n  Post {i}:")
                logger.info(f"    - Text: {post.get('text', 'N/A')[:50]}...")
                logger.info(f"    - Type: {'Video' if post.get('isVideo') else 'Image/Text'}")
                logger.info(f"    - Likes: {post.get('likes', 0):,}")
                logger.info(f"    - Comments: {post.get('comments', 0):,}")
                logger.info(f"    - Shares: {post.get('shares', 0):,}")
                logger.info(f"    - URL: {post.get('url', 'N/A')[:60]}...")
        
        # Sample videos
        if videos:
            logger.info(f"\n🎥 Sample Videos (first 2):")
            for i, video in enumerate(videos[:2], 1):
                logger.info(f"\n  Video {i}:")
                logger.info(f"    - Text: {video.get('text', 'N/A')[:50]}...")
                logger.info(f"    - Likes: {video.get('likes', 0):,}")
                logger.info(f"    - URL: {video.get('url', 'N/A')[:60]}...")
        
        logger.info("\n" + "="*80)
        logger.info("🎉 TEST PASSED!")
        logger.info("="*80 + "\n")
        
        return True
        
    except Exception as e:
        logger.error("\n" + "="*80)
        logger.error("❌ TEST FAILED")
        logger.error("="*80)
        logger.error(f"Error: {str(e)}", exc_info=True)
        logger.error("="*80 + "\n")
        return False


def main():
    """Main test function."""
    # Test URL (HuyK - Kim Hoàn profile)
    test_url = "https://www.facebook.com/100063721444581"
    
    logger.info("\n" + "🚀 FACEBOOK HYBRID SERVICE TEST - UPDATED VERSION")
    logger.info("="*80 + "\n")
    
    success = test_facebook_analysis(test_url)
    
    if success:
        logger.info("✅ All tests passed!")
        logger.info("\n💡 Next steps:")
        logger.info("  1. Open browser: http://localhost:3001/dashboard/facebook/analyze")
        logger.info("  2. Enter URL: https://www.facebook.com/100063721444581")
        logger.info("  3. Click 'Phân tích'")
        logger.info("  4. View results with Videos/Images tabs")
    else:
        logger.error("❌ Tests failed. Check errors above.")
    
    logger.info("\n" + "="*80 + "\n")


if __name__ == "__main__":
    main()
