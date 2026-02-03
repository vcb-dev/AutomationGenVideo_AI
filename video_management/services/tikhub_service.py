"""
TikHub API service for getting user profile stats.

TikHub provides aggregated statistics that TikTok Official API doesn't,
including total views across all videos.
"""

import logging
import requests
from typing import Dict, Any, Optional
from django.conf import settings

logger = logging.getLogger(__name__)


class TikHubService:
    """Service for interacting with TikHub API."""
    
    def __init__(self):
        """Initialize TikHub service."""
        self.api_key = getattr(settings, 'TIKHUB_API_KEY', '')
        if not self.api_key:
            raise ValueError("TIKHUB_API_KEY not configured in settings")
        
        self.base_url = "https://api.tikhub.io/api/v1"
        self.timeout = 30
        
        logger.info("TikHub service initialized")
    
    def _make_request(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Make HTTP request to TikHub API.
        
        Args:
            endpoint: API endpoint (e.g., '/tiktok/web/fetch_user_profile')
            params: Query parameters
            
        Returns:
            JSON response from API
            
        Raises:
            requests.RequestException: If request fails
        """
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        try:
            logger.info(f"TikHub API request: {endpoint}")
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"TikHub API response received successfully")
            return data
            
        except requests.RequestException as e:
            logger.error(f"TikHub API request failed: {str(e)}")
            raise
    
    def get_user_profile(self, username: str) -> Dict[str, Any]:
        """
        Get TikTok user profile with aggregate stats.
        
        Args:
            username: TikTok username (with or without @)
            
        Returns:
            Dictionary containing:
                - username: str
                - display_name: str
                - avatar_url: str
                - follower_count: int
                - following_count: int
                - total_likes: int (total likes across all videos)
                - total_views: int (total views across all videos)
                - video_count: int (total number of videos)
                
        Raises:
            requests.RequestException: If API request fails
        """
        # Remove @ if present
        clean_username = username.replace('@', '').strip()
        
        try:
            # Call TikHub API - use production endpoint for better accuracy
            logger.info(f"🔍 Fetching TikHub profile for username: {clean_username}")
            response = self._make_request(
                '/tiktok/web/fetch_user_profile',
                params={'uniqueId': clean_username}
            )
            
            # Check response status
            if response.get('code') != 200:
                raise Exception(f"TikHub API error: {response.get('message', 'Unknown error')}")
            
            # Extract data from response
            data = response.get('data', {})
            user_info_wrapper = data.get('userInfo', {})
            user = user_info_wrapper.get('user', {})
            stats = user_info_wrapper.get('stats', {})
            
            # Verify we got the right user
            response_username = user.get('uniqueId', '')
            logger.info(f"📥 TikHub returned profile for: {response_username} (requested: {clean_username})")
            
            # Map to standardized format
            profile = {
                'username': clean_username,
                'display_name': user.get('nickname', '') or user.get('uniqueId', clean_username),
                'avatar_url': user.get('avatarThumb', '') or user.get('avatarMedium', ''),
                'follower_count': stats.get('followerCount', 0),
                'following_count': stats.get('followingCount', 0),
                'total_likes': stats.get('heart', 0) or stats.get('heartCount', 0),
                'total_views': 0,  # TikTok doesn't provide total views
                'video_count': stats.get('videoCount', 0),
                'sec_uid': user.get('secUid', ''),
                'user_id': user.get('id', ''),
            }
            
            logger.info(f"✅ TikHub profile for @{clean_username}: {profile['video_count']} videos, {profile['total_likes']:,} likes, {profile['follower_count']:,} followers")
            return profile
            
        except Exception as e:
            logger.error(f"Failed to get user profile for {clean_username}: {str(e)}")
            raise
    
    def get_user_videos(
        self,
        username: str,
        max_results: int = 20,
        cursor: Optional[str] = None,
        sec_uid: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get videos from a TikTok user.
        
        Args:
            username: TikTok username
            max_results: Maximum number of videos to fetch
            cursor: Pagination cursor
            sec_uid: User's secondary UID (preferred for reliability)
            
        Returns:
            Dictionary containing videos and pagination info
        """
        clean_username = username.replace('@', '').strip()
        
        # Prefer sec_user_id if available, otherwise try uniqueId
        params = {
            'count': min(max_results, 30), # Reduce count slightly to be safe
            'cursor': cursor or 0,
            'uniqueId': clean_username
        }
        
        if sec_uid:
            params['sec_user_id'] = sec_uid
        
        try:
            response = self._make_request(
                '/tiktok/web/fetch_user_post',
                params=params
            )
            
            data = response.get('data', {})
            
            return {
                'videos': data.get('videos', []) or data.get('itemList', []),
                'cursor': data.get('cursor', ''),
                'has_more': data.get('hasMore', False) or data.get('has_more', False)
            }
            
        except Exception as e:
            logger.error(f"Failed to get videos for {clean_username}: {str(e)}")
            raise

    def search_videos(
        self,
        keyword: str,
        search_type: str = 'keyword', # 'keyword' or 'hashtag'
        sort_type: int = 0, # 0=general, 1=user, 2=music/video
        cursor: int = 0,
        count: int = 20
    ) -> Dict[str, Any]:
        """
        Search TikTok videos by keyword or hashtag.
        
        Args:
            keyword: Search term
            search_type: 'keyword' or 'hashtag'
            sort_type: Sort order
            cursor: Pagination cursor
            count: Number of results
            
        Returns:
            Dictionary containing search results
        """
        try:
            endpoint = '/tiktok/web/search_item'
            
            # If hashtag search, ensure keyword starts with # or use different logic if API requires
            # TikHub typically handles hashtag search via search_item with keyword starting with #
            # Or dedicated endpoint /tiktok/web/challenge/detail for hashtag info
            
            if search_type == 'hashtag' and not keyword.startswith('#'):
                keyword = f"#{keyword}"
            
            logger.info(f"🔍 Searching TikHub: {keyword} (Type: {search_type}, Cursor: {cursor})")
            
            params = {
                'keyword': keyword,
                'sort_type': sort_type,
                'publish_time': 0, # All time
                'cursor': cursor,
                'count': count
            }
            
            response = self._make_request(endpoint, params=params)
            
            data = response.get('data', {})
            
            item_list = data.get('item_list', []) or []
            has_more = data.get('has_more', False)
            next_cursor = data.get('cursor', cursor + count)
            
            # Normalize list
            normalized_videos = []
            for item in item_list:
                # Type check to ensure it's a video object (sometimes search returns users/music)
                if item.get('type') == 1: # 1 is usually video item
                   item = item.get('item', {}) 
                
                # TikHub sometimes wraps item
                if 'video' in item:
                     # It's likely a video object already
                     pass
                
                # Basic normalization for frontend
                # We can reuse normalize_video_data but might need field adjustments
                normalized_videos.append(item)
                
            return {
                'videos': normalized_videos,
                'cursor': next_cursor,
                'has_more': has_more
            }

        except Exception as e:
            logger.error(f"Failed to search videos for {keyword}: {str(e)}")
            raise

    def normalize_video_data(self, video_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize video data from TikHub to standard format.
        
        Args:
            video_data: Raw video data from TikHub
            
        Returns:
            Dictionary matching ScrapedVideo model fields
        """
        try:
            # Extract basic info
            video_id = str(video_data.get('id', '')) or str(video_data.get('video_id', ''))
            desc = video_data.get('desc', '') or video_data.get('title', '')
            
            # Statistics
            stats = video_data.get('stats', {}) or video_data.get('statistics', {})
            likes = stats.get('diggCount', 0) or stats.get('digg_count', 0)
            views = stats.get('playCount', 0) or stats.get('play_count', 0)
            comments = stats.get('commentCount', 0) or stats.get('comment_count', 0)
            shares = stats.get('shareCount', 0) or stats.get('share_count', 0)
            
            # Author info
            author = video_data.get('author', {})
            author_sec_uid = author.get('secUid', '')
            
            # Video URLs
            video_info = video_data.get('video', {})
            play_addr = video_info.get('playAddr', '') or video_info.get('play_addr', '')
            cover = video_info.get('cover', '') or video_info.get('origin_cover', '')
            
            # Create standard URL
            # Standard TikTok video URL format: https://www.tiktok.com/@{username}/video/{video_id}
            # UnqiueId might be missing in some endpoints, use a placeholder if needed, but usually available in author
            unique_id = author.get('uniqueId', '') or 'user'
            url = f"https://www.tiktok.com/@{unique_id}/video/{video_id}"

            # Timestamp
            create_time = video_data.get('createTime', 0) or video_data.get('create_time', 0)
            from datetime import datetime
            import pytz
            
            published_at = datetime.now(pytz.UTC)
            if create_time:
                try:
                    # Timestamp is usually in seconds for TikTok
                    published_at = datetime.fromtimestamp(int(create_time), pytz.UTC)
                except Exception:
                    pass

            return {
                'platform': 'tiktok', # Assuming TikHub mainly for TikTok
                'video_id': video_id,
                'title': desc[:255], # Truncate if too long (though usually short)
                'description': desc,
                'url': url,
                'cover_url': cover,
                'download_url': play_addr,
                'duration': video_info.get('duration', 0),
                'width': video_info.get('width', 0),
                'height': video_info.get('height', 0),
                'view_count': int(views),
                'like_count': int(likes),
                'comment_count': int(comments),
                'share_count': int(shares),
                'published_at': published_at,
                'author_name': author.get('nickname', ''),
                'author_id': author.get('id', ''),
                'raw_data': video_data
            }
        except Exception as e:
            logger.error(f"Error normalizing TikHub video data: {e}")
            return {}


# Singleton instance
_tikhub_service = None


def get_tikhub_service() -> TikHubService:
    """Get or create TikHub service instance."""
    global _tikhub_service
    if _tikhub_service is None:
        _tikhub_service = TikHubService()
    return _tikhub_service
