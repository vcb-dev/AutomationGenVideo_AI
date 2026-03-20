"""
Facebook Hybrid Service - Combines Graph API and Apify.

This service automatically detects whether a Facebook URL is a Page or User Profile,
then uses the appropriate API:
- Pages/Groups: Facebook Graph API (accurate followers + posts count)
- User Profiles: Apify (posts list only, no followers count)
"""

import logging
import re
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from urllib.parse import quote
from django.conf import settings

from ..models import Platform
from .facebook_graph_service import FacebookGraphService
from .apify_service import ApifyScraperService

logger = logging.getLogger(__name__)


class FacebookHybridService:
    """
    Hybrid service that intelligently routes between Graph API and Apify.
    """
    
    # URL patterns for detection
    PAGE_PATTERNS = [
        r'facebook\.com/pages/',
        r'facebook\.com/pg/',
        r'facebook\.com/groups/',
    ]
    
    PROFILE_PATTERNS = [
        r'facebook\.com/profile\.php\?id=',
        r'facebook\.com/\d{15,}',  # Numeric IDs (15+ digits) are usually profiles
    ]
    
    def __init__(self):
        """Initialize hybrid service."""
        self.graph_service = None
        self.apify_service = None
        
        # Initialize Graph API if configured
        try:
            self.graph_service = FacebookGraphService()
            logger.info("✅ Graph API initialized")
        except Exception as e:
            logger.warning(f"⚠️  Graph API not available: {str(e)}")
        
        # Initialize Apify (cần platform để ApifyScraperService init thành công)
        try:
            self.apify_service = ApifyScraperService(Platform.FACEBOOK, 'posts')
            logger.info("✅ Apify initialized")
        except Exception as e:
            logger.warning(f"⚠️  Apify not available: {str(e)}")
    
    def detect_facebook_type(self, url: str) -> Tuple[str, str]:
        """
        Detect if URL is a Page, Group, or User Profile.
        
        Args:
            url: Facebook URL
            
        Returns:
            Tuple of (type, identifier)
            - type: 'page', 'group', or 'profile'
            - identifier: page ID, group ID, or username
        """
        url = url.strip().lower()
        
        # Check for explicit page patterns
        for pattern in self.PAGE_PATTERNS:
            if re.search(pattern, url):
                # Extract page ID or username
                page_id = self._extract_identifier(url)
                return ('page', page_id)
        
        # Check for explicit profile patterns
        for pattern in self.PROFILE_PATTERNS:
            if re.search(pattern, url):
                profile_id = self._extract_identifier(url)
                return ('profile', profile_id)
        
        # Check for groups
        if '/groups/' in url:
            group_id = self._extract_identifier(url)
            return ('group', group_id)
        
        # Default: Try to determine by ID format
        identifier = self._extract_identifier(url)
        
        # If it's a long numeric ID (15+ digits), likely a profile
        if identifier.isdigit() and len(identifier) >= 15:
            return ('profile', identifier)
        
        # Otherwise, assume it's a page (safer default for Graph API)
        return ('page', identifier)
    
    def _extract_identifier(self, url: str) -> str:
        """
        Extract page ID, username, or profile ID from URL.
        
        Args:
            url: Facebook URL
            
        Returns:
            Identifier (page name, username, or numeric ID)
        """
        # Remove protocol and www
        url = re.sub(r'https?://(www\.)?', '', url)
        
        # Extract from profile.php?id=
        profile_match = re.search(r'profile\.php\?id=(\d+)', url)
        if profile_match:
            return profile_match.group(1)
        
        # Extract from /pages/name/ID or /pg/name/ID
        page_match = re.search(r'/(?:pages|pg)/[^/]+/(\d+)', url)
        if page_match:
            return page_match.group(1)
        
        # Extract from /groups/ID
        group_match = re.search(r'/groups/([^/?]+)', url)
        if group_match:
            return group_match.group(1)
        
        # Extract username or ID from facebook.com/username
        username_match = re.search(r'facebook\.com/([^/?]+)', url)
        if username_match:
            return username_match.group(1)
        
        # Fallback: return cleaned URL
        return url.split('/')[-1].split('?')[0]
    
    def get_facebook_data(
        self,
        url: str,
        max_posts: int = 20,
        force_method: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get Facebook data using the appropriate API.
        
        Args:
            url: Facebook URL
            max_posts: Maximum number of posts to fetch
            force_method: Force specific method ('graph' or 'apify')
            
        Returns:
            Dictionary with:
            {
                'type': 'page' | 'profile' | 'group',
                'method': 'graph_api' | 'apify',
                'identifier': str,
                'name': str,
                'followers_count': int | None,
                'posts_count': int | None,
                'posts': list,
                'metadata': dict
            }
        """
        try:
            # Detect type
            fb_type, identifier = self.detect_facebook_type(url)
            logger.info(f"🔍 Detected: {fb_type} - {identifier}")
            
            # Determine method
            if force_method:
                method = force_method
                logger.info(f"🔧 Forced method: {method}")
            elif fb_type in ['page', 'group'] and self.graph_service:
                method = 'graph'
            else:
                method = 'apify'
            
            logger.info(f"📡 Using method: {method}")
            
            # Fetch data
            if method == 'graph':
                return self._fetch_via_graph_api(fb_type, identifier, max_posts)
            else:
                return self._fetch_via_apify(fb_type, identifier, max_posts)
                
        except Exception as e:
            logger.error(f"❌ Failed to fetch Facebook data: {str(e)}", exc_info=True)
            raise
    
    def _fetch_via_graph_api(
        self,
        fb_type: str,
        identifier: str,
        max_posts: int
    ) -> Dict[str, Any]:
        """
        Fetch data using Facebook Graph API.
        
        Args:
            fb_type: 'page' or 'group'
            identifier: Page/Group ID
            max_posts: Max posts to fetch
            
        Returns:
            Standardized data dictionary
        """
        if not self.graph_service:
            raise ValueError("Graph API not configured")
        
        logger.info(f"📊 Fetching via Graph API: {identifier}")
        
        # Get page metadata
        metadata = self.graph_service.get_page_metadata(identifier)
        
        result = {
            'type': fb_type,
            'method': 'graph_api',
            'identifier': identifier,
            'name': metadata.get('name', ''),
            'followers_count': metadata.get('followers_count'),
            'posts_count': metadata.get('posts_count'),
            'posts': [],  # Graph API doesn't fetch posts list by default
            'metadata': {
                'fan_count': metadata.get('fan_count'),
                'category': metadata.get('category', ''),
                'about': metadata.get('about', ''),
                'website': metadata.get('website', ''),
                'picture_url': metadata.get('picture_url', ''),
            }
        }
        
        logger.info(f"✅ Graph API: {result['name']} - {result['followers_count']:,} followers, {result['posts_count']:,} posts")
        return result
    
    def _fetch_via_apify(
        self,
        fb_type: str,
        identifier: str,
        max_posts: int
    ) -> Dict[str, Any]:
        """
        Fetch data using Apify.
        
        Args:
            fb_type: 'page', 'group', or 'profile'
            identifier: Page/Profile ID or username
            max_posts: Max posts to fetch
            
        Returns:
            Standardized data dictionary
        """
        if not self.apify_service:
            raise ValueError("Apify not configured")
        
        logger.info(f"🕷️  Fetching via Apify: {identifier}")
        
        # Build Facebook URL (encode để xử lý khoảng trắng/unicode - Apify cần URL hợp lệ)
        id_clean = (identifier or '').strip()
        if not id_clean:
            raise ValueError("Facebook identifier is empty")
        id_encoded = quote(id_clean, safe='.-_')
        fb_url = f"https://www.facebook.com/{id_encoded}"
        
        from apify_client import ApifyClient
        client = ApifyClient(settings.APIFY_API_TOKEN)
        
        # 1. Fetch Page Stats (Followers) if it's a page
        followers_count = None
        page_likes = None
        
        if fb_type == 'page':
            try:
                page_actor_id = getattr(settings, 'APIFY_ACTORS', {}).get('facebook_page', 'apify/facebook-pages-scraper')
                logger.info(f"📊 Fetching page stats via {page_actor_id}: {identifier}")
                
                page_run_input = {
                    "startUrls": [{"url": fb_url}],
                    "maxItems": 1
                }
                
                page_run = client.actor(page_actor_id).call(run_input=page_run_input)
                
                if page_run and page_run.get('status') == 'SUCCEEDED':
                    page_items = list(client.dataset(page_run['defaultDatasetId']).iterate_items())
                    if page_items:
                        page_data = page_items[0]
                        followers_count = page_data.get('followers') or page_data.get('followersCount')
                        page_likes = page_data.get('likes') or page_data.get('likesCount')
                        logger.info(f"   found {followers_count} followers, {page_likes} likes")
            except Exception as e:
                logger.warning(f"⚠️  Failed to fetch page stats: {str(e)}")

        # 2. Fetch posts via Apify Actor (startUrls: array of {url} - format chuẩn Apify)
        actor_id = getattr(settings, 'APIFY_ACTORS', {}).get('facebook_posts', 'apify/facebook-posts-scraper')
        
        run_input = {
            "startUrls": [{"url": fb_url}],
            "resultsLimit": max_posts,
            "maxComments": 0,          # Bỏ qua quét chi tiết nội dung comment -> Tăng tốc ĐÁNG KỂ
            "maxReplies": 0,           # Không quét reply
            "scrapeAbout": False,      # Không quét trang giới thiệu
            "scrapeComments": False,   # Tắt hẳn tính năng lấy text comments nếu actor hỗ trợ
            # Nếu actor yêu cầu concurrency: "maxConcurrency": 20
        }
        
        logger.info(f"🚀 Starting Apify actor: {actor_id} | fb_url={fb_url}")
        run = client.actor(actor_id).call(run_input=run_input)
        
        # Fetch results
        items = []
        if run and run.get('status') == 'SUCCEEDED':
            for item in client.dataset(run["defaultDatasetId"]).iterate_items():
                items.append(item)
        
        logger.info(f"📦 Fetched {len(items)} items from Apify")
        
        # Extract user info from first post
        user_name = ''
        user_profile_pic = ''
        user_profile_url = ''
        
        if items and len(items) > 0:
            # DEBUG: Log first item keys to understand structure
            first_key_sample = {k: v for k, v in items[0].items() if k in ['likes', 'likesCount', 'reactionCount', 'comments', 'commentsCount', 'shares', 'shareCount', 'timestamp', 'time']}
            logger.info(f"🔍 First item sample stats: {first_key_sample}")
            
            first_post = items[0]
            user_info = first_post.get('user', {})
            user_name = user_info.get('name', '') or first_post.get('pageName', '')
            user_profile_pic = user_info.get('profilePic', '')
            user_profile_url = user_info.get('profileUrl', '')
        
        # Process posts
        processed_posts = []
        videos = []
        images = []
        
        for item in items:
            # Calculate timestamp if missing
            timestamp = item.get('timestamp')
            
            # Try to parse 'time' if timestamp is missing
            if not timestamp and item.get('time'):
                try:
                    time_str = item.get('time')
                    if time_str and isinstance(time_str, str):
                        # Handle basic ISO format
                        if time_str.endswith('Z'):
                            time_str = time_str[:-1] + '+00:00'
                        dt = datetime.fromisoformat(time_str)
                        timestamp = dt.timestamp()
                except Exception:
                    timestamp = 0
            
            # Robust Extraction of Stats
            likes = item.get('likes') or item.get('likesCount') or item.get('reactionCount') or 0
            comments = item.get('comments') or item.get('commentsCount') or item.get('commentCount') or 0
            shares = item.get('shares') or item.get('sharesCount') or item.get('shareCount') or 0
            views = item.get('views') or item.get('viewCount') or item.get('viewsCount') or 0

            # Helper to parse int safe
            def parse_int_safe(val):
                try:
                    if isinstance(val, str):
                        # Remove non-numeric chars like '1.2K' -> needs more complex parsing, but basic int('') is fine for now
                        val = re.sub(r'[^\d]', '', val)
                        if not val: return 0
                    return int(val)
                except:
                    return 0

            # Extract basic info
            post = {
                'id': item.get('postId', ''),
                'text': item.get('text', ''),
                'url': item.get('url', ''),
                'time': item.get('time', ''),
                'timestamp': timestamp or 0,
                'likes': parse_int_safe(likes),
                'comments': parse_int_safe(comments),
                'shares': parse_int_safe(shares),
                'views': parse_int_safe(views),
                'isVideo': item.get('isVideo', False),
            }
            
            # Extract media
            media_list = item.get('media', [])
            if media_list and len(media_list) > 0:
                media_item = media_list[0]
                post['thumbnail'] = media_item.get('thumbnail', '')
                
                # If it's a video
                if post['isVideo']:
                    video_data = {
                        'id': post['id'],
                        'url': post['url'],
                        'thumbnail': post['thumbnail'],
                        'text': post['text'],
                        'likes': post['likes'],
                        'comments': post['comments'],
                        'shares': post['shares'],
                        'views': post['views'],
                        'time': post['time'],
                        'timestamp': post['timestamp']
                    }
                    videos.append(video_data)
                else:
                    # It's an image
                    image_data = {
                        'id': post['id'],
                        'url': post['url'],
                        'thumbnail': post['thumbnail'],
                        'text': post['text'],
                        'likes': post['likes'],
                        'comments': post['comments'],
                        'shares': post['shares'],
                        'views': post['views'],
                        'time': post['time'],
                        'timestamp': post['timestamp']
                    }
                    images.append(image_data)
            
            processed_posts.append(post)
        
        result = {
            'type': fb_type,
            'method': 'apify',
            'identifier': identifier,
            'name': user_name,
            'followers_count': followers_count,
            'posts_count': len(processed_posts),  # Only fetched posts, not total
            'posts': processed_posts,
            'videos': videos,
            'images': images,
            'metadata': {
                'user_profile_pic': user_profile_pic,
                'user_profile_url': user_profile_url,
                'note': 'Followers fetched via facebook-pages-scraper' if followers_count else 'Followers not available',
                'fetched_posts': len(processed_posts),
                'videos_count': len(videos),
                'images_count': len(images),
                'page_likes': page_likes
            }
        }
        
        logger.info(f"✅ Apify: {result['name']} - {followers_count} followers, {len(processed_posts)} posts")
        return result
    
    def get_available_methods(self) -> Dict[str, bool]:
        """
        Check which methods are available.
        
        Returns:
            Dictionary with availability status
        """
        return {
            'graph_api': self.graph_service is not None,
            'apify': self.apify_service is not None,
        }


# Singleton instance
_hybrid_service = None


def get_facebook_hybrid_service() -> FacebookHybridService:
    """Get or create hybrid service instance."""
    global _hybrid_service
    if _hybrid_service is None:
        _hybrid_service = FacebookHybridService()
    return _hybrid_service
