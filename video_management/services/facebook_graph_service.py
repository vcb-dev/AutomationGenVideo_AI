"""
Facebook Graph API Service.

This service uses Facebook Graph API to fetch page metadata including:
- Followers count
- Total posts count
- Page information

Unlike Apify, this provides accurate, official data directly from Facebook.
"""

import logging
import requests
from typing import Dict, Any, Optional
from django.conf import settings

logger = logging.getLogger(__name__)


class FacebookGraphService:
    """
    Facebook Graph API service for fetching page metadata.
    
    This service provides accurate followers count and posts count
    that Apify's facebook-posts-scraper doesn't provide.
    """
    
    BASE_URL = "https://graph.facebook.com/v18.0"
    
    def __init__(self):
        """Initialize Facebook Graph API service."""
        self.app_id = getattr(settings, 'FACEBOOK_APP_ID', '')
        self.app_secret = getattr(settings, 'FACEBOOK_APP_SECRET', '')
        self.access_token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
        
        if not self.app_id or not self.app_secret:
            raise ValueError("FACEBOOK_APP_ID and FACEBOOK_APP_SECRET must be configured")
        
        # If no access token, generate app access token
        if not self.access_token:
            self.access_token = self._generate_app_access_token()
        
        logger.info("Initialized Facebook Graph API service")
    
    def _generate_app_access_token(self) -> str:
        """
        Generate app access token using app ID and secret.
        
        This token can access public page data without user login.
        
        Returns:
            Access token string
        """
        try:
            url = f"{self.BASE_URL}/oauth/access_token"
            params = {
                'client_id': self.app_id,
                'client_secret': self.app_secret,
                'grant_type': 'client_credentials'
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            token = data.get('access_token', '')
            
            if token:
                logger.info("✅ Generated app access token successfully")
                return token
            else:
                raise ValueError("No access token in response")
                
        except Exception as e:
            logger.error(f"Failed to generate app access token: {str(e)}")
            raise
    
    def get_page_metadata(self, page_id: str) -> Dict[str, Any]:
        """
        Get page metadata including followers and posts count.
        
        Args:
            page_id: Facebook page ID or username
            
        Returns:
            Dictionary with page metadata:
            {
                'page_id': str,
                'name': str,
                'followers_count': int,
                'fan_count': int,  # Same as followers
                'posts_count': int,
                'category': str,
                'about': str,
                'website': str,
                'picture_url': str
            }
        """
        try:
            logger.info(f"🔍 Fetching metadata for page: {page_id}")
            
            url = f"{self.BASE_URL}/{page_id}"
            
            # Request fields
            fields = [
                'id',
                'name',
                'followers_count',
                'fan_count',
                'posts.limit(0).summary(true)',  # Get total count without fetching posts
                'category',
                'about',
                'website',
                'picture'
            ]
            
            params = {
                'fields': ','.join(fields),
                'access_token': self.access_token
            }
            
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            
            # Extract data
            result = {
                'page_id': data.get('id', page_id),
                'name': data.get('name', ''),
                'followers_count': data.get('followers_count', 0),
                'fan_count': data.get('fan_count', 0),
                'posts_count': 0,
                'category': data.get('category', ''),
                'about': data.get('about', ''),
                'website': data.get('website', ''),
                'picture_url': ''
            }
            
            # Get posts count from summary
            if 'posts' in data and 'summary' in data['posts']:
                result['posts_count'] = data['posts']['summary'].get('total_count', 0)
            
            # Get picture URL
            if 'picture' in data and 'data' in data['picture']:
                result['picture_url'] = data['picture']['data'].get('url', '')
            
            logger.info(f"✅ Page metadata retrieved:")
            logger.info(f"  - Name: {result['name']}")
            logger.info(f"  - Followers: {result['followers_count']:,}")
            logger.info(f"  - Posts: {result['posts_count']:,}")
            
            return result
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403:
                logger.error("❌ Access denied. Check your access token permissions.")
                logger.error("   Required: pages_read_engagement, pages_show_list")
            elif e.response.status_code == 404:
                logger.error(f"❌ Page not found: {page_id}")
            else:
                logger.error(f"❌ HTTP error: {e.response.status_code} - {e.response.text}")
            raise
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch page metadata: {str(e)}", exc_info=True)
            raise
    
    def get_page_posts(self, page_id: str, max_results: int = 100, since_date: Optional[str] = None) -> list:
        """
        Get posts from a Facebook page.
        
        Args:
            page_id: Facebook page ID or username
            max_results: Maximum number of posts to fetch
            since_date: Fetch posts since this date (YYYY-MM-DD)
            
        Returns:
            List of posts with engagement data
        """
        try:
            logger.info(f"📝 Fetching posts for page: {page_id}")
            
            url = f"{self.BASE_URL}/{page_id}/posts"
            
            # Request fields for each post
            fields = [
                'id',
                'message',
                'created_time',
                'permalink_url',
                'full_picture',
                'attachments{media,type,url}',
                'likes.summary(true)',
                'comments.summary(true)',
                'shares',
                'reactions.summary(true)',
                'type'
            ]
            
            params = {
                'fields': ','.join(fields),
                'limit': min(max_results, 100),
                'access_token': self.access_token
            }
            
            # Add date filter if provided
            if since_date:
                from datetime import datetime
                try:
                    dt = datetime.strptime(since_date, '%Y-%m-%d')
                    params['since'] = int(dt.timestamp())
                    logger.info(f"Filtering posts since: {since_date}")
                except:
                    pass
            
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            posts = data.get('data', [])
            
            # Normalize posts data
            normalized_posts = []
            for post in posts:
                normalized = self._normalize_post(post)
                if normalized:
                    normalized_posts.append(normalized)
            
            logger.info(f"✅ Fetched {len(normalized_posts)} posts")
            return normalized_posts
            
        except Exception as e:
            logger.error(f"Failed to fetch page posts: {str(e)}", exc_info=True)
            return []
    
    def _normalize_post(self, post: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize Graph API post data to common format.
        
        Args:
            post: Raw post data from Graph API
            
        Returns:
            Normalized post dictionary
        """
        try:
            from datetime import datetime
            
            # Extract basic info
            post_id = post.get('id', '')
            message = post.get('message', '')
            created_time = post.get('created_time', '')
            permalink = post.get('permalink_url', '')
            
            # Parse timestamp
            timestamp = 0
            if created_time:
                try:
                    dt = datetime.fromisoformat(created_time.replace('Z', '+00:00'))
                    timestamp = int(dt.timestamp())
                except:
                    pass
            
            # Extract engagement metrics
            likes_count = 0
            if 'likes' in post and 'summary' in post['likes']:
                likes_count = post['likes']['summary'].get('total_count', 0)
            
            comments_count = 0
            if 'comments' in post and 'summary' in post['comments']:
                comments_count = post['comments']['summary'].get('total_count', 0)
            
            shares_count = 0
            if 'shares' in post:
                shares_count = post['shares'].get('count', 0)
            
            # Determine if it's a video
            is_video = False
            video_url = ''
            thumbnail = post.get('full_picture', '')
            
            post_type = post.get('type', '')
            if post_type == 'video':
                is_video = True
            
            # Check attachments for video
            if 'attachments' in post and 'data' in post['attachments']:
                for attachment in post['attachments']['data']:
                    if attachment.get('type') == 'video_inline':
                        is_video = True
                        if 'media' in attachment and 'source' in attachment['media']:
                            video_url = attachment['media']['source']
                    # Get better thumbnail from attachments
                    if 'media' in attachment and 'image' in attachment['media']:
                        if 'src' in attachment['media']['image']:
                            thumbnail = attachment['media']['image']['src']
            
            return {
                'id': post_id,
                'video_id': post_id,
                'title': message[:200] if message else 'No message',
                'description': message,
                'timestamp': timestamp,
                'url': permalink,
                'video_url': video_url,
                'download_url': video_url,
                'thumbnail': thumbnail,
                'thumbnail_url': thumbnail,
                'is_video': is_video,
                'isVideo': is_video,
                'likes': likes_count,
                'like_count': likes_count,
                'likes_count': likes_count,
                'comments': comments_count,
                'comment_count': comments_count,
                'comments_count': comments_count,
                'shares': shares_count,
                'share_count': shares_count,
                'shares_count': shares_count,
                'views': 0,
                'view_count': 0,
                'views_count': 0,
                'raw_data': post
            }
            
        except Exception as e:
            logger.error(f"Failed to normalize post: {str(e)}")
            return None
    
    def get_page_insights(self, page_id: str, metrics: Optional[list] = None) -> Dict[str, Any]:
        """
        Get page insights (analytics).
        
        Note: This requires a Page Access Token, not App Access Token.
        
        Args:
            page_id: Facebook page ID
            metrics: List of metrics to fetch (e.g., ['page_impressions', 'page_engaged_users'])
            
        Returns:
            Dictionary with insights data
        """
        if not metrics:
            metrics = ['page_impressions', 'page_engaged_users', 'page_views_total']
        
        try:
            url = f"{self.BASE_URL}/{page_id}/insights"
            params = {
                'metric': ','.join(metrics),
                'access_token': self.access_token
            }
            
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            return data
            
        except Exception as e:
            logger.error(f"Failed to fetch page insights: {str(e)}")
            raise
    
    def test_connection(self) -> bool:
        """
        Test if the service is properly configured and can connect to Facebook.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Try to get info about Facebook's own page
            url = f"{self.BASE_URL}/facebook"
            params = {
                'fields': 'id,name',
                'access_token': self.access_token
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"✅ Connection test successful! Got data: {data}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Connection test failed: {str(e)}")
            return False


# Convenience function
def get_facebook_page_metadata(page_id: str) -> Dict[str, Any]:
    """
    Convenience function to get Facebook page metadata.
    
    Args:
        page_id: Facebook page ID or username
        
    Returns:
        Dictionary with page metadata
    """
    service = FacebookGraphService()
    return service.get_page_metadata(page_id)
