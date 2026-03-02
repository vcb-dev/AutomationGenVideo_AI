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
            
            # Build actor input - STRICTLY OPTIMIZED to 1 event per video
            actor_input = {
                "searchTermsOrHashtags": [clean_term],
                "sortBy": sort_by,
                "publishTime": publish_time,
                "maxItemsPerUrl": max_posts,
                "maxPosts": max_posts,
                # --- Disable all paid Add-ons (Events) ---
                "shouldDownloadCovers": False,      # Disable Cover Download Event ($0.50/1000)
                "shouldDownloadVideos": False,      # Disable Video Download Event ($1.00/1000)
                "shouldDownloadMusic": False,
                "scrapeAdditionalUserInfo": False, # Disable User Info Event ($0.50/1000)
                "scrapePlayCount": False,           # Disable Play Count Event ($0.50/1000)
                "scrapeUserPostCount": False,
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
        Handles various schemas from different versions of the Apify actor.
        """
        # Video ID
        video_id = raw_data.get('id', '') or raw_data.get('aweme_id', '')
        
        # Author info - Handles both 'author' (old) and 'authorMeta' (new)
        author = raw_data.get('authorMeta') or raw_data.get('author') or {}
        author_username = author.get('username') or author.get('unique_id') or author.get('uid', '')
        author_name = author.get('name') or author.get('nickname') or author_username
        
        # Avatar extraction
        author_avatar = ''
        if 'avatarThumb' in author:
            author_avatar = author['avatarThumb']
        elif 'avatar_thumb' in author:
            avatar_thumb = author.get('avatar_thumb', {})
            if isinstance(avatar_thumb, dict) and avatar_thumb.get('url_list'):
                author_avatar = avatar_thumb['url_list'][0]
        
        # Stats - Handles camelCase (new) and snake_case (old)
        statistics = raw_data.get('statistics', {}) or {}
        views_count = statistics.get('playCount') or statistics.get('play_count')
        likes_count = statistics.get('diggCount') or statistics.get('digg_count')
        comments_count = statistics.get('commentCount') or statistics.get('comment_count', 0)
        shares_count = statistics.get('shareCount') or statistics.get('share_count', 0)
        collect_count = statistics.get('collectCount') or statistics.get('collect_count', 0)
        
        # Video info - Handles 'videoMeta' (new) and 'video' (old)
        video_meta = raw_data.get('videoMeta') or raw_data.get('video') or {}
        video_url = raw_data.get('url') or raw_data.get('share_url', '')
        
        # Download URL
        download_url = ''
        # New schema has playUrl in videoMeta
        if 'playUrl' in video_meta:
            download_url = video_meta['playUrl']
        # Old schema has play_addr in video
        elif 'play_addr' in video_meta:
            play_addr = video_meta.get('play_addr', {})
            if isinstance(play_addr, dict) and play_addr.get('url_list'):
                download_url = play_addr['url_list'][0]
        
        # Thumbnail extraction
        thumbnail_url = ''
        if 'cover' in video_meta:
            thumbnail_url = video_meta['cover']
        elif 'cover_url' in raw_data: # Alternative fallback
            thumbnail_url = raw_data['cover_url']
        elif 'thumb' in raw_data:
            thumbnail_url = raw_data['thumb']
            
        # Caption/Description
        caption = raw_data.get('text') or raw_data.get('desc', '')
        
        # Hashtags
        hashtags = []
        # New schema: list of objects in 'hashtags'
        raw_hashtags = raw_data.get('hashtags', [])
        if raw_hashtags and isinstance(raw_hashtags, list):
            for tag in raw_hashtags:
                if isinstance(tag, dict) and tag.get('name'):
                    hashtags.append(tag['name'])
                elif isinstance(tag, str):
                    hashtags.append(tag)
        
        # Old schema: 'text_extra'
        if not hashtags:
            text_extra = raw_data.get('text_extra', []) or []
            for tag in text_extra:
                if isinstance(tag, dict) and tag.get('hashtag_name'):
                    hashtags.append(tag['hashtag_name'])
        
        # Music info
        music = raw_data.get('musicMeta') or raw_data.get('music') or {}
        music_title = music.get('name') or music.get('title', '')
        music_author = music.get('author') or music.get('author', '')
        music_url = music.get('playUrl') or ''
        
        if not music_url and 'play_url' in music:
            play_url = music.get('play_url', {})
            if isinstance(play_url, dict) and play_url.get('url_list'):
                music_url = play_url['url_list'][0]
        
        # Duration
        duration = video_meta.get('duration', 0)
        
        # Published time
        create_time = raw_data.get('createTime') or raw_data.get('create_time')
        published_at = None
        if create_time:
            try:
                # Some versions might return string ISO format
                if isinstance(create_time, str):
                    published_at = datetime.fromisoformat(create_time.replace('Z', '+00:00'))
                else:
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
            'collect_count': int(collect_count) if collect_count else 0,
            'author_id': author.get('id') or author.get('uid', ''),
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
