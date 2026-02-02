"""
Test Instagram Apify Service

This script tests the Instagram Apify service to verify:
1. Profile data fetching (followers, bio, etc.)
2. Posts and reels fetching
3. Data normalization and statistics
"""

import os
import sys
import django
import json
from pathlib import Path

# Fix Unicode encoding for Windows console
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# Setup Django
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.instagram_apify_service import InstagramApifyService


def test_profile_only(username: str):
    """Test fetching profile information only."""
    print(f"\n{'='*60}")
    print(f"TEST 1: Fetching Profile Info for @{username}")
    print(f"{'='*60}\n")
    
    try:
        service = InstagramApifyService()
        profile = service.get_profile_info(username)
        
        print("[OK] Profile Data Retrieved:")
        print(f"  Username: {profile['username']}")
        print(f"  Full Name: {profile['fullName']}")
        print(f"  Biography: {profile['biography'][:100]}..." if len(profile.get('biography', '')) > 100 else f"  Biography: {profile.get('biography', '')}")
        print(f"  Followers: {profile['followersCount']:,}")
        print(f"  Following: {profile['followingCount']:,}")
        print(f"  Posts: {profile['postsCount']:,}")
        print(f"  Verified: {'YES' if profile['isVerified'] else 'NO'}")
        print(f"  Private: {'YES' if profile['isPrivate'] else 'NO'}")
        print(f"  Profile Picture URL: {profile.get('profilePicUrl', 'NOT FOUND')}")
        
        return profile
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return None


def test_posts_and_reels(username: str, max_results: int = 10):
    """Test fetching posts and reels."""
    print(f"\n{'='*60}")
    print(f"TEST 2: Fetching Posts & Reels for @{username} (limit: {max_results})")
    print(f"{'='*60}\n")
    
    try:
        service = InstagramApifyService()
        content = service.get_user_posts_and_reels(username, max_results)
        
        print(f"✅ Retrieved {len(content)} content items\n")
        
        # Separate posts and reels
        posts = [c for c in content if c['content_type'] == 'post']
        reels = [c for c in content if c['content_type'] == 'reel']
        
        print(f"📊 Content Breakdown:")
        print(f"  Posts: {len(posts)}")
        print(f"  Reels: {len(reels)}")
        print()
        
        # Show first 3 items
        print("📝 Sample Content (first 3 items):")
        for i, item in enumerate(content[:3], 1):
            print(f"\n  {i}. {item['content_type'].upper()} ({item['media_type']})")
            print(f"     ID: {item['short_code']}")
            print(f"     Caption: {item['caption'][:80]}..." if len(item['caption']) > 80 else f"     Caption: {item['caption']}")
            print(f"     Likes: {item['likes_count']:,}")
            print(f"     Comments: {item['comments_count']:,}")
            print(f"     Views: {item['video_view_count']:,}")
            print(f"     Posted: {item['timestamp']}")
            print(f"     Hashtags: {', '.join(item['hashtags'][:5])}" if item['hashtags'] else "     Hashtags: None")
        
        return content
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


def test_complete_data(username: str, max_posts: int = 20):
    """Test fetching complete Instagram data."""
    print(f"\n{'='*60}")
    print(f"TEST 3: Fetching COMPLETE Data for @{username}")
    print(f"{'='*60}\n")
    
    try:
        service = InstagramApifyService()
        data = service.get_complete_user_data(username, max_posts)
        
        profile = data['profile']
        content = data['content']
        stats = data['stats']
        
        print("✅ Complete Data Retrieved\n")
        
        print("👤 PROFILE:")
        print(f"  {profile['fullName']} (@{profile['username']})")
        print(f"  Followers: {profile['followersCount']:,}")
        print(f"  Posts: {profile['postsCount']:,}")
        print()
        
        print("📊 CONTENT STATISTICS:")
        print(f"  Total Content: {stats['total_content']}")
        print(f"  Posts: {stats['total_posts']}")
        print(f"  Reels: {stats['total_reels']}")
        print()
        
        print("💡 ENGAGEMENT METRICS:")
        print(f"  Total Likes: {stats['total_likes']:,}")
        print(f"  Total Comments: {stats['total_comments']:,}")
        print(f"  Total Views: {stats['total_views']:,}")
        print(f"  Avg Likes/Post: {stats['avg_likes']:,.2f}")
        print(f"  Avg Comments/Post: {stats['avg_comments']:,.2f}")
        print(f"  Avg Views/Post: {stats['avg_views']:,.2f}")
        print(f"  Avg Engagement Rate: {stats['avg_engagement_rate']:.2f}%")
        print()
        
        # Save to file for inspection
        output_file = f"instagram_{username}_data.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        
        print(f"💾 Full data saved to: {output_file}")
        
        return data
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("INSTAGRAM APIFY SERVICE TEST SUITE")
    print("="*60)
    
    # Test username - you can change this
    # Use a public Instagram account for testing
    test_username = input("\nEnter Instagram username to test (without @): ").strip()
    
    if not test_username:
        print("❌ No username provided. Using default: 'instagram'")
        test_username = "instagram"
    
    # Test 1: Profile only
    profile = test_profile_only(test_username)
    
    if not profile:
        print("\n❌ Profile test failed. Stopping tests.")
        return
    
    # Test 2: Posts and Reels (small sample)
    content = test_posts_and_reels(test_username, max_results=5)
    
    if not content:
        print("\n⚠️ Content test failed. Skipping complete data test.")
        return
    
    # Test 3: Complete data
    complete_data = test_complete_data(test_username, max_posts=20)
    
    print("\n" + "="*60)
    print("✅ ALL TESTS COMPLETED")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
