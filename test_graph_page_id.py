"""
Test Facebook Graph API with Page ID instead of username.

Sometimes using Page ID works better than username.
"""

import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.facebook_graph_service import FacebookGraphService

def test_with_page_id():
    """Test with numeric Page ID."""
    service = FacebookGraphService()
    
    # Try with a known public page ID
    # Example: Facebook's own page
    test_cases = [
        ("20531316728", "Facebook Official Page"),
        ("100063721444581", "User Profile (will fail)"),
    ]
    
    for page_id, description in test_cases:
        print(f"\n{'='*70}")
        print(f"Testing: {description}")
        print(f"Page ID: {page_id}")
        print('='*70)
        
        try:
            result = service.get_page_metadata(page_id)
            print(f"✅ SUCCESS!")
            print(f"  Name: {result['name']}")
            print(f"  Followers: {result['followers_count']:,}")
            print(f"  Posts: {result['posts_count']:,}")
        except Exception as e:
            print(f"❌ FAILED: {str(e)}")

if __name__ == "__main__":
    test_with_page_id()
