
import os
import django
import sys

# Setup Django environment
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.tikhub_service import get_tikhub_service

def test_tikhub():
    username = 'chib.ao'  # Username bạn đang test
    print(f"Testing TikHub for user: {username}")
    
    try:
        service = get_tikhub_service()
        profile = service.get_user_profile(username)
        
        print("\n✅ TikHub Result:")
        print(f"Username: {profile['username']}")
        print(f"Display Name: {profile['display_name']}")
        print(f"Followers: {profile['follower_count']:,} (Raw: {profile['follower_count']})")
        print(f"Likes: {profile['total_likes']:,}")
        print(f"Videos: {profile['video_count']}")
        
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")

if __name__ == '__main__':
    test_tikhub()
