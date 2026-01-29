"""
Test script to check Apify actor limits and capabilities
"""
import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.apify_service import ApifyScraperService
from video_management.models import Platform

def test_apify_limits():
    """Test Apify actor with different max_results values"""
    
    # Test with TikTok
    scraper = ApifyScraperService(platform=Platform.TIKTOK)
    
    print("=" * 60)
    print("APIFY CONFIGURATION TEST")
    print("=" * 60)
    print(f"Platform: {scraper.platform}")
    print(f"Actor ID: {scraper.actor_id}")
    print(f"Max Results Limit (from settings): {scraper.max_results_limit}")
    print(f"Timeout: {scraper.timeout}s")
    print("=" * 60)
    
    # Test building actor input with different values
    test_username = "test_user"
    
    print("\nTesting actor input generation:")
    print("-" * 60)
    
    for max_results in [100, 1000, 5000, 9999]:
        actor_input = scraper._build_actor_input(
            keyword="",
            max_results=max_results,
            username=test_username
        )
        print(f"\nmax_results={max_results}:")
        print(f"  resultsPerPage: {actor_input.get('resultsPerPage')}")
        print(f"  postsCount: {actor_input.get('postsCount')}")
        print(f"  profiles: {actor_input.get('profiles')}")
    
    print("\n" + "=" * 60)
    print("RECOMMENDATIONS:")
    print("=" * 60)
    print("1. Apify free-tiktok-scraper actor may have limits:")
    print("   - Free tier: ~100-500 videos per run")
    print("   - Paid tier: Can fetch more (depends on credits)")
    print("\n2. To fetch ALL videos from a channel (1892+):")
    print("   - Ensure you have enough Apify credits")
    print("   - Set APIFY_MAX_RESULTS=9999 (already done)")
    print("   - Increase APIFY_TIMEOUT if needed (currently 1800s = 30min)")
    print("\n3. Current configuration:")
    print("   - Should request up to 9999 videos")
    print("   - Actual results depend on Apify actor limits")
    print("   - Check Apify console for run details and errors")
    print("=" * 60)

if __name__ == '__main__':
    test_apify_limits()
