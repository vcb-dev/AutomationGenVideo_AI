"""
TikTok Web Scraper - Direct HTML parsing to get user stats.
This bypasses API cache issues by scraping TikTok's public web pages.
"""
import logging
import requests
from bs4 import BeautifulSoup
import json
import re
from typing import Dict, Any

logger = logging.getLogger(__name__)


class TikTokWebScraper:
    """Scrape TikTok user stats from public web pages."""
    
    def __init__(self):
        self.base_url = 'https://www.tiktok.com'
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
    
    def get_user_stats(self, username: str) -> Dict[str, Any]:
        """
        Scrape user stats from TikTok web page.
        
        Args:
            username: TikTok username (without @)
            
        Returns:
            Dict with user stats
        """
        clean_username = username.replace('@', '').strip()
        url = f"{self.base_url}/@{clean_username}"
        
        logger.info(f"🌐 Scraping TikTok web page: {url}")
        
        try:
            response = requests.get(url, headers=self.headers, timeout=15)
            response.raise_for_status()
            
            # Parse HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Find SIGI_STATE script tag (contains user data)
            script_tag = soup.find('script', id='SIGI_STATE')
            
            if not script_tag:
                # Try alternative: __UNIVERSAL_DATA_FOR_REHYDRATION__
                script_tags = soup.find_all('script')
                for script in script_tags:
                    if 'UserModule' in script.text or '__UNIVERSAL_DATA_FOR_REHYDRATION__' in script.text:
                        script_tag = script
                        break
            
            if not script_tag:
                raise Exception("Could not find user data in page")
            
            # Extract JSON data
            data_text = script_tag.string
            data = json.loads(data_text)
            
            # Navigate to user data
            user_module = data.get('UserModule', {})
            users = user_module.get('users', {})
            
            # Find user by username
            user_data = None
            for user_id, user_info in users.items():
                if user_info.get('uniqueId', '').lower() == clean_username.lower():
                    user_data = user_info
                    break
            
            if not user_data:
                raise Exception(f"User {clean_username} not found in page data")
            
            # Extract stats
            stats = user_data.get('stats', {})
            
            result = {
                'username': clean_username,
                'display_name': user_data.get('nickname', clean_username),
                'avatar_url': user_data.get('avatarThumb', ''),
                'follower_count': stats.get('followerCount', 0),
                'following_count': stats.get('followingCount', 0),
                'total_likes': stats.get('heart', 0) or stats.get('heartCount', 0),
                'video_count': stats.get('videoCount', 0),
                'total_views': 0,  # Not available
            }
            
            logger.info(f"✅ Web scrape success: {result['follower_count']:,} followers, {result['total_likes']:,} likes")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Web scraping failed: {str(e)}")
            raise


def get_tiktok_web_scraper() -> TikTokWebScraper:
    """Get TikTok web scraper instance."""
    return TikTokWebScraper()
