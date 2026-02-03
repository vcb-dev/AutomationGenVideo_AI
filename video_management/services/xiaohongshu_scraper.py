"""
Xiaohongshu Scraper Service using Apify Actor: kuaima/xiaohongshu-search

This service handles keyword search on Xiaohongshu platform.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from django.conf import settings
from apify_client import ApifyClient

logger = logging.getLogger(__name__)


class XiaohongshuScraperService:
    """
    Xiaohongshu scraper using Apify actor.
    
    Supports:
    - Keyword search
    - Sort options (general, hot, latest)
    """
    
    def __init__(self):
        """Initialize Xiaohongshu scraper with Apify client."""
        self.api_token = getattr(settings, 'APIFY_API_TOKEN', '')
        if not self.api_token:
            raise ValueError("APIFY_API_TOKEN not configured")
        
        # Initialize Apify client
        self.client = ApifyClient(self.api_token)
        
        # Get actor ID from settings or use default
        self.actor_id = getattr(
            settings, 
            'APIFY_ACTOR_XIAOHONGSHU', 
            'kuaima/xiaohongshu-search' # fallback
        )
        
        self.timeout = getattr(settings, 'APIFY_TIMEOUT', 300)
        
        logger.info(f"Initialized Xiaohongshu scraper using actor: {self.actor_id}")
    
    def search_notes(
        self,
        search_term: str,
        sort_type: str = 'general',  # 'general', 'hot', 'latest'
        max_posts: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search Xiaohongshu notes by keyword.
        """
        try:
            clean_term = search_term.strip()
            
            # Build actor input based on kuaima/xiaohongshu-search schema
            actor_input = {
                "keywords": [clean_term],
                "sortType": sort_type,
                "maxItems": max_posts,
                "proxyConfig": { "useApifyProxy": True }
            }
            
            logger.info(f"Searching Xiaohongshu - Term: {clean_term}, Sort: {sort_type}, Max: {max_posts}")
            
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
            
            # Fetch items
            items = []
            for item in self.client.dataset(dataset_id).iterate_items():
                items.append(item)
            
            logger.info(f"Retrieved {len(items)} notes from Xiaohongshu")
            
            # Normalize data
            normalized_items = [self._normalize_note_data(item) for item in items]
            
            return normalized_items
            
        except Exception as e:
            logger.error(f"Xiaohongshu search failed: {str(e)}", exc_info=True)
            raise Exception(f"Xiaohongshu search failed: {str(e)}")
    
    def _normalize_note_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Xiaohongshu note data."""
        # Note ID
        note_id = raw_data.get('id', '') or raw_data.get('note_id', '')
        
        # Author
        user = raw_data.get('user', {})
        author_name = user.get('nickname', '') or user.get('name', '')
        author_avatar = user.get('avatar', '') or user.get('image', '')
        author_id = user.get('id', '') or user.get('user_id', '')
        
        # Stats
        likes = raw_data.get('likes', 0) or raw_data.get('liked_count', 0)
        collects = raw_data.get('collects', 0) or raw_data.get('collected_count', 0)
        comments = raw_data.get('comments', 0) or raw_data.get('comment_count', 0)
        shares = raw_data.get('shares', 0) or raw_data.get('share_count', 0)
        
        # Images/Video
        images_list = raw_data.get('images_list', []) or raw_data.get('images', [])
        image_list_urls = [img.get('url', img) if isinstance(img, dict) else img for img in images_list]
        
        cover_url = ''
        if image_list_urls:
            cover_url = image_list_urls[0]
        else:
            cover_url = raw_data.get('cover', {}).get('url', '')
            
        video_url = raw_data.get('video', {}).get('media', {}).get('stream', {}).get('h264', [{}])[0].get('master_url', '')

        # Metadata
        timestamp = raw_data.get('timestamp') or raw_data.get('create_time', 0)
        published_at = None
        if timestamp:
             try:
                published_at = datetime.fromtimestamp(int(timestamp) / 1000).isoformat() # Often ms
             except:
                pass

        return {
            'note_id': str(note_id),
            'type': raw_data.get('type', 'image'),
            'title': raw_data.get('title', ''),
            'desc': raw_data.get('desc', '') or raw_data.get('description', ''),
            'hashtags': [], # Extraction logic dependent on raw format
            'likes_count': int(likes) if likes else 0,
            'views_count': None, # XHS hides view count usually
            'comments_count': int(comments) if comments else 0,
            'collects_count': int(collects) if collects else 0,
            'shares_count': int(shares) if shares else 0,
            'author_name': author_name,
            'author_avatar': author_avatar,
            'author_id': author_id,
            'cover_url': cover_url,
            'video_url': video_url,
            'images_list': image_list_urls,
            'published_at': published_at,
            'note_url': f"https://www.xiaohongshu.com/explore/{note_id}",
            'raw_data': raw_data
        }
