"""
Apify scraper service for all platforms.

This service uses Apify actors to scrape data from TikTok, Instagram,
Facebook, and Douyin.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from django.conf import settings
from apify_client import ApifyClient
from apify_client.clients import ActorClient

from .base_scraper import (
    BaseScraperService,
    ScraperException,
    ScraperTimeoutException,
    ScraperNotFoundException
)
from ..models import Platform

logger = logging.getLogger(__name__)


class ApifyScraperService(BaseScraperService):
    """
    Apify-based scraper service supporting multiple platforms.
    
    This service uses Apify actors to scrape data from various social media
    platforms in a unified way.
    """
    
    def __init__(self, platform: Platform):
        """
        Initialize Apify scraper.
        
        Args:
            platform: Target platform to scrape
            
        Raises:
            ScraperException: If Apify is not configured
        """
        super().__init__(platform)
        
        # Get Apify configuration
        self.api_token = getattr(settings, 'APIFY_API_TOKEN', '')
        if not self.api_token:
            raise ScraperException("APIFY_API_TOKEN not configured")
        
        # Initialize Apify client
        self.client = ApifyClient(self.api_token)
        
        # Get actor ID for platform
        actors = getattr(settings, 'APIFY_ACTORS', {})
        self.actor_id = actors.get(platform.value, '')
        
        if not self.actor_id:
            raise ScraperException(f"No Apify actor configured for {platform}")
        
        self.timeout = getattr(settings, 'APIFY_TIMEOUT', 300)
        self.max_results_limit = getattr(settings, 'APIFY_MAX_RESULTS', 100)
        
        self.logger.info(
            f"Initialized Apify scraper for {platform} "
            f"using actor: {self.actor_id}"
        )
    
    def _build_tiktok_input(
        self,
        keyword: str,
        max_results: int = 20
    ) -> Dict[str, Any]:
        """Build input for TikTok Apify actor."""
        return {
            "searchQueries": [keyword],
            "resultsPerPage": min(max_results, self.max_results_limit),
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False,
            "shouldDownloadSlideshowImages": False,
        }
    
    def _build_instagram_input(
        self,
        keyword: str,
        max_results: int = 20
    ) -> Dict[str, Any]:
        """Build input for Instagram Apify actor."""
        # Clean hashtag - remove # and spaces
        clean_keyword = keyword.replace('#', '').replace(' ', '').strip()
        
        # Use directUrls format which works on free tier
        # Build Instagram hashtag explore URL
        instagram_url = f"https://www.instagram.com/explore/tags/{clean_keyword}/"
        
        return {
            "directUrls": [instagram_url],
            "resultsLimit": min(max_results, self.max_results_limit),
        }
    
    def _build_instagram_reels_input(
        self,
        keyword: str,
        max_results: int = 20
    ) -> Dict[str, Any]:
        """Build input for Instagram Reel Scraper actor."""
        # For hashtag search, we need to use Instagram Hashtag Scraper with resultsType=reels
        # Or search by username if keyword starts with @
        # Remove spaces for proper hashtag/username formatting
        clean_keyword = keyword.replace('#', '').replace('@', '').replace(' ', '').strip()
        
        if keyword.startswith('@'):
            # Search user's reels
            return {
                "username": [clean_keyword],
                "resultsLimit": min(max_results, self.max_results_limit),
            }
        else:
            # Search hashtag reels - use hashtag scraper with reels filter
            return {
                "hashtags": [clean_keyword],
                "resultsLimit": min(max_results, self.max_results_limit),
                "resultsType": "reels",  # Only get reels
                "searchType": "hashtag",
            }
    
    def _build_facebook_input(
        self,
        keyword: str,
        max_results: int = 20
    ) -> Dict[str, Any]:
        """Build input for Facebook Apify actor."""
        return {
            "searchQuery": keyword,
            "maxPosts": min(max_results, self.max_results_limit),
        }
    
    def _build_actor_input(
        self,
        keyword: str,
        max_results: int = 20,
        username: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Build actor input based on platform.
        
        Args:
            keyword: Search keyword
            max_results: Maximum results
            username: Username for user-specific searches
            
        Returns:
            Actor input dictionary
        """
        if self.platform == Platform.TIKTOK:
            if username:
                # For user profile scraping, use different fields
                # Use the larger value to ensure we get as many videos as possible
                max_to_fetch = max(max_results, self.max_results_limit)
                return {
                    "profiles": [username],
                    "resultsPerPage": max_to_fetch,
                    "postsCount": max_to_fetch,  # Some actors use this
                    "shouldDownloadVideos": False,
                }
            return self._build_tiktok_input(keyword, max_results)
        
        elif self.platform == Platform.INSTAGRAM:
            if username:
                # For Instagram profiles, use both resultsLimit and postsLimit
                return {
                    "usernames": [username],
                    "resultsLimit": min(max_results, self.max_results_limit),
                    "postsLimit": min(max_results, self.max_results_limit),  # Alternative field
                }
            return self._build_instagram_input(keyword, max_results)
        
        elif self.platform == Platform.FACEBOOK:
            return self._build_facebook_input(keyword, max_results)
        
        elif self.platform == Platform.DOUYIN:
            # Similar to TikTok
            return self._build_tiktok_input(keyword, max_results)
        
        else:
            raise ScraperException(f"Unsupported platform: {self.platform}")
    
    def run_actor(
        self,
        actor_input: Dict[str, Any],
        timeout: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Run Apify actor and get results.
        
        Args:
            actor_input: Input parameters for the actor
            timeout: Timeout in seconds (uses default if None)
            
        Returns:
            List of results from actor
            
        Raises:
            ScraperException: If actor run fails
            ScraperTimeoutException: If actor times out
        """
        timeout = timeout or self.timeout
        
        try:
            self.logger.info(f"Running Apify actor: {self.actor_id}")
            self.logger.debug(f"Actor input: {actor_input}")
            
            # Run the actor
            run = self.client.actor(self.actor_id).call(
                run_input=actor_input,
                timeout_secs=timeout
            )
            
            # Check run status
            if run['status'] != 'SUCCEEDED':
                error_msg = f"Actor run failed with status: {run['status']}"
                self.logger.error(error_msg)
                raise ScraperException(error_msg)
            
            # Get results from dataset
            dataset_id = run.get('defaultDatasetId')
            if not dataset_id:
                raise ScraperException("No dataset ID in actor run")
            
            self.logger.info(f"Fetching results from dataset: {dataset_id}")
            
            # Fetch all items from dataset
            items = []
            for item in self.client.dataset(dataset_id).iterate_items():
                items.append(item)
            
            self.logger.info(f"Retrieved {len(items)} items from Apify")
            return items
            
        except Exception as e:
            if 'timeout' in str(e).lower():
                raise ScraperTimeoutException(f"Actor run timed out: {str(e)}")
            raise ScraperException(f"Actor run failed: {str(e)}")
    
    def search_videos(
        self,
        keyword: str,
        min_likes: int = 0,
        min_views: int = 0,
        max_results: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Search for videos using Apify.
        
        Args:
            keyword: Search keyword or hashtag
            min_likes: Minimum likes (filtering done after scraping)
            min_views: Minimum views (filtering done after scraping)
            max_results: Maximum results to fetch
            
        Returns:
            List of raw video data from Apify
        """
        try:
            actor_input = {}
            # Special handling for Instagram Hashtag search
            if self.platform == Platform.INSTAGRAM and not keyword.startswith('@') and not keyword.startswith('http'):
                # Switch to hashtag scraper for better results count
                actors = getattr(settings, 'APIFY_ACTORS', {})
                self.actor_id = actors.get('instagram_hashtag', 'apify/instagram-hashtag-scraper')
                self.logger.info(f"Switching to Hashtag Scraper ({self.actor_id}) for keyword: {keyword}")
                
                # Build specific input for hashtag scraper
                clean_keyword = keyword.replace('#', '').replace(' ', '').strip()
                actor_input = {
                    "hashtags": [clean_keyword],
                    "resultsLimit": min(max_results, self.max_results_limit),
                    "searchType": "hashtag"
                }
            else:
                # Default input builder
                actor_input = self._build_actor_input(keyword, max_results)
                
            results = self.run_actor(actor_input)
            
            self.logger.info(
                f"Found {len(results)} videos for keyword: {keyword}"
            )
            
            return results
            
        except Exception as e:
            self.logger.error(f"Search failed: {str(e)}", exc_info=True)
            raise ScraperException(f"Search failed: {str(e)}")
    
    def get_user_videos(
        self,
        username: str,
        max_results: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Get videos from a specific user.
        
        Args:
            username: Username or user ID
            max_results: Maximum results
            
        Returns:
            List of raw video data
        """
        try:
            actor_input = self._build_actor_input(
                keyword="",  # Not used for user searches
                max_results=max_results,
                username=username
            )
            results = self.run_actor(actor_input)
            
            self.logger.info(
                f"Found {len(results)} videos for user: {username}"
            )
            
            return results
            
        except Exception as e:
            self.logger.error(f"User video fetch failed: {str(e)}", exc_info=True)
            raise ScraperException(f"User video fetch failed: {str(e)}")
    
    def normalize_video_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize Apify video data to common format.
        
        Args:
            raw_data: Raw data from Apify actor
            
        Returns:
            Normalized video data
        """
        # Platform-specific normalization
        if self.platform == Platform.TIKTOK or self.platform == Platform.DOUYIN:
            return self._normalize_tiktok_data(raw_data)
        elif self.platform == Platform.INSTAGRAM:
            return self._normalize_instagram_data(raw_data)
        elif self.platform == Platform.FACEBOOK:
            return self._normalize_facebook_data(raw_data)
        else:
            raise ScraperException(f"Unsupported platform: {self.platform}")
    
    def _normalize_tiktok_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize TikTok/Douyin data from Apify free-tiktok-scraper."""
        # Video ID
        video_id = data.get('id', '')
        
        # Author info - in authorMeta object
        author_meta = data.get('authorMeta', {})
        author_username = author_meta.get('name', '') or author_meta.get('uniqueId', '')
        author_name = author_meta.get('nickname', '') or author_username
        
        # Stats - try root first, then 'stats' object
        stats = data.get('stats', {})
        likes_count = data.get('diggCount') or stats.get('diggCount', 0)
        views_count = data.get('playCount') or stats.get('playCount', 0)
        comments_count = data.get('commentCount') or stats.get('commentCount', 0)
        shares_count = data.get('shareCount') or stats.get('shareCount', 0)
        
        # URLs
        video_url = data.get('webVideoUrl', '')
        
        # Download URL - from videoMeta or mediaUrls
        download_url = ''
        video_meta = data.get('videoMeta', {})
        if video_meta and isinstance(video_meta, dict):
            download_url = video_meta.get('downloadAddr', '') or video_meta.get('playAddr', '')
        
        if not download_url:
            media_urls = data.get('mediaUrls', [])
            if media_urls and isinstance(media_urls, list) and len(media_urls) > 0:
                download_url = media_urls[0]
        
        # Thumbnail - from videoMeta cover or authorMeta avatar as fallback
        thumbnail_url = ''
        if video_meta and isinstance(video_meta, dict):
            thumbnail_url = (
                video_meta.get('coverUrl', '') or
                video_meta.get('cover', '') or
                video_meta.get('dynamicCover', '') or
                video_meta.get('originCover', '')
            )
        
        
        # REMOVED Fallback to author avatar. We want video-specific covers only.
        # if not thumbnail_url and author_meta:
        #     thumbnail_url = author_meta.get('avatar', '')
        
        # Music info - from musicMeta
        music_meta = data.get('musicMeta', {})
        music_title = music_meta.get('musicName', '') if music_meta else ''
        music_author = music_meta.get('musicAuthor', '') if music_meta else ''
        
        # Published time
        create_time = data.get('createTime') or data.get('createTimeISO')
        
        return {
            'video_id': str(video_id),
            'title': data.get('text', ''),
            'description': data.get('text', ''),
            'author_username': author_username,
            'author_name': author_name,
            'likes_count': int(likes_count) if likes_count else 0,
            'views_count': int(views_count) if views_count else 0,
            'comments_count': int(comments_count) if comments_count else 0,
            'shares_count': int(shares_count) if shares_count else 0,
            'video_url': video_url,
            'download_url': download_url,
            'thumbnail_url': thumbnail_url,
            'published_at': self._parse_timestamp(create_time),
            'hashtags': [tag.get('name', '') for tag in data.get('hashtags', [])],
            'music_info': {
                'title': music_title,
                'author': music_author,
                'url': '',
            },
            'raw_data': data
        }
    
    def _normalize_instagram_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Instagram data."""
        # Thumbnail fallback logic
        thumbnail = ''
        if data.get('images') and len(data['images']) > 0:
            thumbnail = data['images'][0]
        else:
            thumbnail = data.get('displayUrl', '') or data.get('thumbnailUrl', '')

        # Author fallback logic
        author_username = data.get('ownerUsername', '')
        if not author_username and data.get('owner'):
             author_username = data['owner'].get('username', '')
             
        # Normalize stats (ensure int)
        likes = data.get('likesCount', 0)
        views = data.get('videoViewCount', 0) or data.get('videoPlayCount', 0)

        return {
            'video_id': data.get('id') or data.get('shortCode', ''),
            'title': data.get('caption', ''),
            'description': data.get('caption', ''),
            'author_username': author_username,
            'author_name': data.get('ownerFullName', ''),
            'likes_count': int(likes) if likes else 0,
            'views_count': int(views) if views else 0,
            'comments_count': data.get('commentsCount', 0),
            'shares_count': 0,  # Instagram doesn't provide this
            'video_url': data.get('url') or data.get('postUrl', ''),
            'download_url': data.get('videoUrl', ''),
            'thumbnail_url': thumbnail,
            'published_at': self._parse_timestamp(data.get('timestamp') or data.get('date')),
            'hashtags': data.get('hashtags', []),
            'music_info': {},
            'raw_data': data
        }
    
    def _normalize_facebook_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Facebook data."""
        return {
            'video_id': data.get('postId') or data.get('id', ''),
            'title': data.get('text', ''),
            'description': data.get('text', ''),
            'author_username': data.get('postAuthor', ''),
            'author_name': data.get('postAuthor', ''),
            'likes_count': data.get('likes', 0),
            'views_count': data.get('views', 0),
            'comments_count': data.get('comments', 0),
            'shares_count': data.get('shares', 0),
            'video_url': data.get('postUrl', ''),
            'download_url': data.get('videoUrl', ''),
            'thumbnail_url': data.get('image', ''),
            'published_at': self._parse_timestamp(data.get('time')),
            'hashtags': [],
            'music_info': {},
            'raw_data': data
        }
    
    def _parse_timestamp(self, timestamp: Any) -> Optional[datetime]:
        """
        Parse various timestamp formats to datetime.
        
        Args:
            timestamp: Timestamp in various formats
            
        Returns:
            Datetime object or None
        """
        if not timestamp:
            return None
        
        try:
            # If already datetime
            if isinstance(timestamp, datetime):
                return timestamp
            
            # If Unix timestamp (integer)
            if isinstance(timestamp, (int, float)):
                return datetime.fromtimestamp(timestamp)
            
            # If ISO string
            if isinstance(timestamp, str):
                from dateutil import parser
                return parser.parse(timestamp)
            
            return None
            
        except Exception as e:
            self.logger.warning(f"Failed to parse timestamp {timestamp}: {str(e)}")
            return None


# Factory function to create appropriate scraper
def create_scraper(platform: str, search_type: str = 'posts') -> ApifyScraperService:
    """
    Create a scraper instance for the specified platform.
    
    Args:
        platform: Platform name (tiktok, instagram, facebook, douyin)
        search_type: specific search type (e.g. 'reels', 'posts')
        
    Returns:
        ApifyScraperService instance
        
    Raises:
        ValueError: If platform is not supported
    """
    try:
        platform_enum = Platform[platform.upper()]
        
        # Check for specific scraper variants
        if platform_enum == Platform.INSTAGRAM and search_type == 'reels':
            from .instagram_reels_scraper import create_instagram_reels_scraper
            return create_instagram_reels_scraper()
            
        return ApifyScraperService(platform_enum)
        
    except KeyError:
        raise ValueError(
            f"Unsupported platform: {platform}. "
            f"Supported: {', '.join([p.value for p in Platform])}"
        )
