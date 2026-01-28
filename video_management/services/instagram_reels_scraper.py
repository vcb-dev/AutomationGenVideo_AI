"""
Instagram Reels Scraper Service using Apify Instagram Reel Scraper.

This service provides full engagement data (likes, views, comments) for Instagram Reels.
"""

import logging
from typing import Dict, List, Optional, Any
from django.conf import settings

from .apify_service import ApifyScraperService
from ..models import Platform

logger = logging.getLogger(__name__)


class InstagramReelsScraperService(ApifyScraperService):
    """
    Instagram Reels scraper using apify/instagram-reel-scraper.
    
    This scraper provides:
    - Full engagement data (likes, views, comments, plays)
    - Reels only (no images/carousels)
    - Video download URLs
    - Music metadata
    """
    
    def __init__(self):
        """Initialize Instagram Reels scraper."""
        super().__init__(Platform.INSTAGRAM)
        
        # Override actor ID to use Reel scraper
        actors = getattr(settings, 'APIFY_ACTORS', {})
        self.actor_id = actors.get('instagram_reels', 'apify/instagram-reel-scraper')
        
        self.logger.info(
            f"Initialized Instagram Reels scraper using actor: {self.actor_id}"
        )
    
    def search_videos(
        self,
        keyword: str,
        min_likes: int = 0,
        min_views: int = 0,
        max_results: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Search for Instagram Reels.
        """
        try:
            # Build input using parent method (handles logic for @username vs #hashtag)
            actor_input = self._build_instagram_reels_input(keyword, max_results)
            
            # DYNAMIC ACTOR SELECTION
            actors = getattr(settings, 'APIFY_ACTORS', {})
            
            if "hashtags" in actor_input:
                # Use Hashtag Scraper for hashtags
                # This scraper supports finding reels via hashtag search
                self.actor_id = actors.get('instagram_hashtag', 'apify/instagram-hashtag-scraper')
                self.logger.info(f"Using Hashtag Scraper ({self.actor_id}) for keyword: {keyword}")
            else:
                # Use Reel Scraper for usernames
                # This scraper gets comprehensive reel data for specific users
                self.actor_id = actors.get('instagram_reels', 'apify/instagram-reel-scraper')
                self.logger.info(f"Using Reel Scraper ({self.actor_id}) for username: {keyword}")

            results = self.run_actor(actor_input)
            
            self.logger.info(
                f"Found {len(results)} items for keyword: {keyword}"
            )
            
            return results
            
        except Exception as e:
            self.logger.error(f"Reels search failed: {str(e)}", exc_info=True)
            raise
    
    def normalize_video_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize Instagram Reel data.
        Handles both Reel Scraper and Hashtag Scraper formats.
        """
        # Hashtag scraper uses 'timestamp' or 'date', Reel scraper uses 'timestamp'
        timestamp = raw_data.get('timestamp') or raw_data.get('date')
        
        # Hashtag scraper might use 'displayUrl' instead of 'images' list
        thumbnail = ''
        if raw_data.get('images'):
            thumbnail = raw_data.get('images')[0]
        else:
            thumbnail = raw_data.get('displayUrl', '')
            
        return {
            'video_id': raw_data.get('id') or raw_data.get('shortCode', ''),
            'title': raw_data.get('caption', ''),
            'description': raw_data.get('caption', ''),
            'author_username': raw_data.get('ownerUsername', ''),
            'author_name': raw_data.get('ownerFullName', ''),
            'likes_count': raw_data.get('likesCount', 0),
            'views_count': raw_data.get('videoViewCount', 0) or raw_data.get('videoPlayCount', 0),
            'comments_count': raw_data.get('commentsCount', 0),
            'shares_count': 0,
            'video_url': raw_data.get('url') or raw_data.get('postUrl', ''),
            'download_url': raw_data.get('videoUrl', ''),
            'thumbnail_url': thumbnail,
            'published_at': self._parse_timestamp(timestamp),
            'hashtags': raw_data.get('hashtags', []),
            'music_info': raw_data.get('musicInfo', {}),
            'raw_data': raw_data
        }


# Factory function
def create_instagram_reels_scraper() -> InstagramReelsScraperService:
    """Create Instagram Reels scraper instance."""
    return InstagramReelsScraperService()
