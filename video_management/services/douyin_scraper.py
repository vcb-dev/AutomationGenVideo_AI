"""
Douyin Scraper Service using Apify Actor: natanielsantos/douyin-scraper

This service handles keyword and hashtag search on Douyin platform.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from django.conf import settings
from apify_client import ApifyClient

logger = logging.getLogger(__name__)


class DouyinScraperService:
    """
    Douyin scraper using Apify actor: natanielsantos/douyin-scraper
    
    Supports:
    - Keyword search
    - Hashtag search
    - Pagination (50 videos per request)
    """
    
    def __init__(self):
        """Initialize Douyin scraper with Apify client."""
        self.api_token = getattr(settings, 'APIFY_API_TOKEN', '')
        if not self.api_token:
            raise ValueError("APIFY_API_TOKEN not configured")
        
        # Initialize Apify client
        self.client = ApifyClient(self.api_token)
        
        # Get actor ID from settings or use default
        self.actor_id = getattr(
            settings, 
            'APIFY_ACTOR_DOUYIN', 
            'natanielsantos/douyin-scraper'
        )
        
        self.timeout = getattr(settings, 'APIFY_TIMEOUT', 300)
        
        logger.info(f"Initialized Douyin scraper using actor: {self.actor_id}")
    
    def search_videos(
        self,
        search_term: str,
        search_type: str = 'keyword',  # 'keyword' or 'hashtag'
        max_posts: int = 50,
        sort_by: str = 'general',  # 'general', 'most_liked', 'latest'
        publish_time: str = 'all'  # 'all', 'last_day', 'last_week', 'last_half_year'
    ) -> List[Dict[str, Any]]:
        """
        Search Douyin videos by keyword or hashtag.
        
        Args:
            search_term: Keyword or hashtag to search (without # for hashtag)
            search_type: 'keyword' or 'hashtag'
            max_posts: Maximum number of posts to fetch (default 50)
            sort_by: Sort order - 'general', 'most_liked', 'latest'
            publish_time: Time filter - 'all', 'last_day', 'last_week', 'last_half_year'
            
        Returns:
            List of video data dictionaries
            
        Raises:
            Exception: If scraping fails
        """
        try:
            # Clean search term
            clean_term = search_term.strip()
            
            # For hashtag, ensure it starts with #
            if search_type == 'hashtag' and not clean_term.startswith('#'):
                clean_term = f'#{clean_term}'
            
            # Build actor input
            # Updated to match current actor schema requirements
            actor_input = {
                "searchTermsOrHashtags": [clean_term],
                "sortBy": sort_by,
                "publishTime": publish_time,
                "maxItemsPerUrl": max_posts,  # Required by new schema
                "maxPosts": max_posts,        # Keep for backward compatibility
                "shouldDownloadCovers": True,
                "shouldDownloadVideos": False, # We only need metadata mostly
                "shouldDownloadMusic": False,
                "shouldDownloadAuthors": True
            }
            
            logger.info(f"Searching Douyin - Type: {search_type}, Term: {clean_term}, Max: {max_posts}")
            logger.debug(f"Actor input: {actor_input}")
            
            # Run the actor
            run = self.client.actor(self.actor_id).call(
                run_input=actor_input,
                timeout_secs=self.timeout
            )
            
            # Check run status
            if run['status'] != 'SUCCEEDED':
                error_msg = f"Actor run failed with status: {run['status']}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            # Get results from dataset
            dataset_id = run.get('defaultDatasetId')
            if not dataset_id:
                raise Exception("No dataset ID in actor run")
            
            logger.info(f"Fetching results from dataset: {dataset_id}")
            
            # Fetch all items from dataset
            items = []
            for item in self.client.dataset(dataset_id).iterate_items():
                items.append(item)
            
            logger.info(f"Retrieved {len(items)} videos from Douyin")
            
            # Normalize data
            normalized_items = [self._normalize_video_data(item) for item in items]
            
            return normalized_items
            
        except Exception as e:
            logger.error(f"Douyin search failed: {str(e)}", exc_info=True)
            raise Exception(f"Douyin search failed: {str(e)}")
    
    def _normalize_video_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize Douyin video data to common format.
        
        Note: Douyin may not return view/like counts due to privacy settings.
        These fields will be None if not available.
        
        Args:
            raw_data: Raw data from Apify actor
            
        Returns:
            Normalized video data
        """
        # Video ID
        video_id = raw_data.get('id', '') or raw_data.get('aweme_id', '')
        
        # Author info
        author = raw_data.get('author', {}) or {}
        author_username = author.get('unique_id', '') or author.get('uid', '')
        author_name = author.get('nickname', '') or author_username
        author_avatar = author.get('avatar_thumb', {}).get('url_list', [''])[0] if author.get('avatar_thumb') else ''
        
        # Stats - may be None if not available
        statistics = raw_data.get('statistics', {}) or {}
        views_count = statistics.get('play_count')  # May be None
        likes_count = statistics.get('digg_count')  # May be None
        comments_count = statistics.get('comment_count', 0)
        shares_count = statistics.get('share_count', 0)
        
        # Video info
        video = raw_data.get('video', {}) or {}
        video_url = raw_data.get('share_url', '')
        
        # Download URL - get highest quality
        download_url = ''
        play_addr = video.get('play_addr', {})
        if play_addr and play_addr.get('url_list'):
            download_url = play_addr['url_list'][0]
        
        # Thumbnail
        thumbnail_url = ''
        cover = video.get('cover', {})
        if cover and cover.get('url_list'):
            thumbnail_url = cover['url_list'][0]
        
        # Caption/Description
        caption = raw_data.get('desc', '')
        
        # Hashtags
        hashtags = []
        text_extra = raw_data.get('text_extra', []) or []
        for tag in text_extra:
            if tag.get('hashtag_name'):
                hashtags.append(tag['hashtag_name'])
        
        # Music info
        music = raw_data.get('music', {}) or {}
        music_title = music.get('title', '')
        music_author = music.get('author', '')
        music_url = ''
        play_url = music.get('play_url', {})
        if play_url and play_url.get('url_list'):
            music_url = play_url['url_list'][0]
        
        # Duration
        duration = video.get('duration', 0)
        
        # Published time
        create_time = raw_data.get('create_time')
        published_at = None
        if create_time:
            try:
                published_at = datetime.fromtimestamp(int(create_time))
            except:
                pass
        
        return {
            'video_id': str(video_id),
            'duration': duration,
            'caption': caption,
            'description': caption,
            'hashtags': hashtags,
            'views_count': int(views_count) if views_count is not None else None,
            'likes_count': int(likes_count) if likes_count is not None else None,
            'comments_count': int(comments_count) if comments_count else 0,
            'shares_count': int(shares_count) if shares_count else 0,
            'author_id': author.get('uid', ''),
            'author_username': author_username,
            'author_name': author_name,
            'author_avatar': author_avatar,
            'video_url': video_url,
            'download_url': download_url,
            'thumbnail_url': thumbnail_url,
            'music_title': music_title,
            'music_author': music_author,
            'music_url': music_url,
            'published_at': published_at.isoformat() if published_at else None,
            'raw_data': raw_data
        }
