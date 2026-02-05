"""
Base scraper service with common functionality.

This module provides the foundation for all platform-specific scrapers,
including retry logic, error handling, and result normalization.
"""

import logging
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from django.conf import settings
from django.utils import timezone
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log
)
import requests

from ..models import Platform, SearchHistory, ScrapedVideo, SearchStatus

logger = logging.getLogger(__name__)


class ScraperException(Exception):
    """Base exception for scraper errors."""
    pass


class ScraperTimeoutException(ScraperException):
    """Raised when scraper operation times out."""
    pass


class ScraperRateLimitException(ScraperException):
    """Raised when rate limit is hit."""
    pass


class ScraperNotFoundException(ScraperException):
    """Raised when resource is not found."""
    pass


class BaseScraperService(ABC):
    """
    Abstract base class for all platform scrapers.
    
    Provides common functionality like:
    - Retry logic with exponential backoff
    - Error handling and logging
    - Result normalization
    - Cache management
    """
    
    def __init__(self, platform: Platform):
        """
        Initialize scraper service.
        
        Args:
            platform: The social media platform this scraper handles
        """
        self.platform = platform
        self.logger = logging.getLogger(f"{__name__}.{platform}")
        self.max_retries = getattr(settings, 'SCRAPER_MAX_RETRIES', 3)
        self.retry_delay = getattr(settings, 'SCRAPER_RETRY_DELAY', 2)
        self.cache_ttl = getattr(settings, 'SEARCH_CACHE_TTL', 3600)
    
    @abstractmethod
    def search_videos(
        self,
        keyword: str,
        min_likes: int = 0,
        min_views: int = 0,
        max_results: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Search for videos on the platform.
        
        Args:
            keyword: Search keyword or hashtag
            min_likes: Minimum likes filter
            min_views: Minimum views filter
            max_results: Maximum number of results to return
            
        Returns:
            List of video data dictionaries
            
        Raises:
            ScraperException: If search fails
        """
        pass
    
    @abstractmethod
    def get_user_videos(
        self,
        username: str,
        max_results: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Get videos from a specific user/channel.
        
        Args:
            username: Username or channel ID
            max_results: Maximum number of results to return
            
        Returns:
            List of video data dictionaries
            
        Raises:
            ScraperException: If fetch fails
        """
        pass
    
    @abstractmethod
    def normalize_video_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize platform-specific video data to common format.
        
        Args:
            raw_data: Raw video data from platform API
            
        Returns:
            Normalized video data dictionary with standard fields
        """
        pass
    
    def check_cache(
        self,
        keyword: str,
        min_likes: int = 0,
        min_views: int = 0,
        max_results: int = 20
    ) -> Optional[SearchHistory]:
        """
        Check if valid cached results exist for this search.
        
        Args:
            keyword: Search keyword
            min_likes: Minimum likes filter
            min_views: Minimum views filter
            max_results: Minimum required results
            
        Returns:
            SearchHistory object if valid cache exists, None otherwise
        """
        try:
            # Only use cache if it was created with at least the requested max_results
            # or if it was created very recently
            cache_entry = SearchHistory.objects.filter(
                platform=self.platform,
                keyword=keyword,
                min_likes=min_likes,
                min_views=min_views,
                status=SearchStatus.COMPLETED,
                expires_at__gt=timezone.now(),
                max_results__gte=max_results  # Ensure cache has enough data depth
            ).order_by('-created_at').first()
            
            if cache_entry and not cache_entry.is_expired():
                self.logger.info(f"Cache hit for keyword: {keyword} (results={cache_entry.results_count})")
                return cache_entry
            
            self.logger.info(f"Cache miss for keyword: {keyword}")
            return None
            
        except Exception as e:
            self.logger.error(f"Error checking cache: {str(e)}")
            return None
    
    def create_search_history(
        self,
        keyword: str,
        min_likes: int = 0,
        min_views: int = 0,
        max_results: int = 20,
        task_id: Optional[str] = None
    ) -> SearchHistory:
        """
        Create a new search history entry.
        
        Args:
            keyword: Search keyword
            min_likes: Minimum likes filter
            min_views: Minimum views filter
            max_results: Maximum results
            task_id: Celery task ID if async
            
        Returns:
            Created SearchHistory object
        """
        expires_at = timezone.now() + timedelta(seconds=self.cache_ttl)
        
        return SearchHistory.objects.create(
            platform=self.platform,
            keyword=keyword,
            min_likes=min_likes,
            min_views=min_views,
            max_results=max_results,
            task_id=task_id,
            expires_at=expires_at,
            status=SearchStatus.PROCESSING
        )
    
    def save_videos(
        self,
        videos_data: List[Dict[str, Any]],
        search_history: Optional[SearchHistory] = None
    ) -> List[ScrapedVideo]:
        """
        Save scraped videos to database.
        
        Args:
            videos_data: List of normalized video data
            search_history: Associated search history entry
            
        Returns:
            List of created/updated ScrapedVideo objects
        """
        saved_videos = []
        
        for video_data in videos_data:
            try:
                if not video_data.get('video_id'):
                    self.logger.warning("Skipping video with no ID")
                    continue

                # Truncate fields that might exceed DB limits
                video_url = video_data.get('video_url', '')[:999]
                download_url = video_data.get('download_url', '')[:999]
                thumbnail_url = video_data.get('thumbnail_url', '')[:999]

                video, created = ScrapedVideo.objects.update_or_create(
                    video_id=video_data['video_id'],
                    defaults={
                        'platform': self.platform,
                        'title': video_data.get('title', ''),
                        'description': video_data.get('description', ''),
                        'author_username': video_data.get('author_username', ''),
                        'author_name': video_data.get('author_name', ''),
                        'likes_count': video_data.get('likes_count', 0),
                        'views_count': video_data.get('views_count', 0),
                        'comments_count': video_data.get('comments_count', 0),
                        'shares_count': video_data.get('shares_count', 0),
                        'video_url': video_url,
                        'download_url': download_url,
                        'thumbnail_url': thumbnail_url,
                        'published_at': video_data.get('published_at'),
                        'hashtags': video_data.get('hashtags', []),
                        'music_info': video_data.get('music_info', {}),
                        'raw_data': video_data.get('raw_data', {}),
                        'search_history': search_history
                    }
                )
                saved_videos.append(video)
                
                action = "Created" if created else "Updated"
                self.logger.debug(f"{action} video: {video.video_id}")
                
            except Exception as e:
                self.logger.error(f"Error saving video {video_data.get('video_id')}: {str(e)}")
                continue
        
        self.logger.info(f"Saved {len(saved_videos)} videos to database")
        return saved_videos
    
    def filter_results(
        self,
        videos: List[Dict[str, Any]],
        keyword: str = "",
        min_likes: int = 0,
        min_views: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Filter video results based on criteria and relevance.
        
        Args:
            videos: List of video data
            keyword: The search keyword (for relevance check)
            min_likes: Minimum likes threshold
            min_views: Minimum views threshold
            
        Returns:
            Filtered list of videos
        """
        self.logger.info(
            f"Filtering {len(videos)} videos. "
            f"Criteria: min_likes={min_likes}, min_views={min_views}, keyword='{keyword}'"
        )
            
        filtered = []
        
        # Pre-process keyword for comparison
        # 1. Standard lower
        kw_standard = keyword.lower().strip()
        # 2. No spaces (e.g., "trang suc" -> "trangsuc" for hashtag matching)
        kw_nospace = kw_standard.replace(' ', '')
        
        for v in videos:
            # 1. METRICS FILTER
            likes = v.get('likes_count', 0)
            views = v.get('views_count', 0)
            
            if likes < min_likes or views < min_views:
                self.logger.debug(
                    f"Dropping video {v.get('video_id')} (Metrics): "
                    f"likes={likes}<{min_likes} or views={views}<{min_views}"
                )
                continue
                
            # 2. RELEVANCE FILTER (Relaxed)
            # We trust TikTok's search engine to return relevant results.
            # Strict filtering rejects too many semantically related videos.
            # We only filter by metrics now.
            
            # (Strict logic removed to improve yield and speed)
            
            filtered.append(v)
            
        self.logger.info(
            f"Filtered {len(videos)} videos to {len(filtered)} "
        )
        return filtered
    
    def execute_search(
        self,
        keyword: str,
        min_likes: int = 0,
        min_views: int = 0,
        max_results: int = 20,
        use_cache: bool = True,
        save_to_db: bool = True
    ) -> Dict[str, Any]:
        """
        Execute a complete search operation with caching and error handling.
        
        Args:
            keyword: Search keyword
            min_likes: Minimum likes filter
            min_views: Minimum views filter
            max_results: Maximum results
            use_cache: Whether to use cached results
            save_to_db: Whether to save results to database
            
        Returns:
            Dictionary with search results and metadata
        """
        start_time = time.time()
        
        try:
            # Check cache first
            if use_cache:
                cached = self.check_cache(keyword, min_likes, min_views, max_results)
                if cached:
                    return {
                        'success': True,
                        'cached': True,
                        'results': cached.raw_results,
                        'count': cached.results_count,
                        'execution_time': 0,
                        'search_id': cached.id
                    }
            
            # Create search history entry
            search_history = self.create_search_history(
                keyword=keyword,
                min_likes=min_likes,
                min_views=min_views,
                max_results=max_results
            )
            
            try:
                # Perform actual search
                self.logger.info(f"Searching {self.platform} for: {keyword}")
                raw_results = self.search_videos(
                    keyword=keyword,
                    min_likes=min_likes,
                    min_views=min_views,
                    max_results=max_results
                )
                
                # Normalize results
                normalized_results = [
                    self.normalize_video_data(video)
                    for video in raw_results
                ]
                
                # Filter results
                filtered_results = self.filter_results(
                    normalized_results,
                    keyword=keyword,
                    min_likes=min_likes,
                    min_views=min_views
                )
                
                execution_time = time.time() - start_time
                
                # Save to database
                if save_to_db:
                    self.save_videos(filtered_results, search_history)
                
                # Update search history
                search_history.mark_completed(filtered_results, execution_time)
                
                return {
                    'success': True,
                    'cached': False,
                    'results': filtered_results,
                    'count': len(filtered_results),
                    'execution_time': execution_time,
                    'search_id': search_history.id
                }
                
            except Exception as e:
                # Mark search as failed
                search_history.mark_failed(str(e))
                raise
                
        except Exception as e:
            execution_time = time.time() - start_time
            self.logger.error(f"Search failed: {str(e)}", exc_info=True)
            
            return {
                'success': False,
                'cached': False,
                'error': str(e),
                'results': [],
                'count': 0,
                'execution_time': execution_time
            }
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.RequestException, ScraperTimeoutException)),
        before_sleep=before_sleep_log(logger, logging.WARNING)
    )
    def make_request(
        self,
        url: str,
        method: str = 'GET',
        **kwargs
    ) -> requests.Response:
        """
        Make HTTP request with retry logic.
        
        Args:
            url: Request URL
            method: HTTP method
            **kwargs: Additional arguments for requests
            
        Returns:
            Response object
            
        Raises:
            ScraperException: If request fails after retries
        """
        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            self.logger.error(f"Request failed: {str(e)}")
            raise ScraperException(f"Request failed: {str(e)}") from e
