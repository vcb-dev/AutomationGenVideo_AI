"""
Base scraper service with common functionality.

This module provides the foundation for all platform-specific scrapers,
including retry logic, error handling, and result normalization.
"""

import logging
import time
import random
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
        min_comments: int = 0,
        max_results: int = 20,
        search_mode: str = "hashtag",
        session_id: str = None
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
        max_results: int = 20,
        search_mode: str = "hashtag"
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
            search_mode_val = (search_mode or "hashtag").strip().lower()
            cache_entry = SearchHistory.objects.filter(
                platform=self.platform,
                keyword=keyword,
                min_likes=min_likes,
                min_views=min_views,
                search_mode=search_mode_val,
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
        task_id: Optional[str] = None,
        search_mode: str = "hashtag"
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
        
        search_mode_val = (search_mode or "hashtag").strip().lower()
        return SearchHistory.objects.create(
            platform=self.platform,
            keyword=keyword,
            min_likes=min_likes,
            min_views=min_views,
            max_results=max_results,
            task_id=task_id,
            search_mode=search_mode_val,
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
        
        def _safe_url(val: Any, max_len: int = 999) -> str:
            """Extract URL string from value (handles dict from Apify e.g. {uri: '...'}) and truncate."""
            if val is None:
                return ''
            if isinstance(val, str):
                return val[:max_len]
            if isinstance(val, dict):
                s = val.get('url') or val.get('uri') or val.get('src') or val.get('link') or ''
                return (s if isinstance(s, str) else str(s) if s else '')[:max_len]
            return str(val)[:max_len] if val else ''

        for video_data in videos_data:
            try:
                if not video_data.get('video_id'):
                    self.logger.warning("Skipping video with no ID")
                    continue

                # Truncate fields (Apify may return dict e.g. {uri: "..."} - slicing dict causes unhashable type: 'slice')
                video_url = _safe_url(video_data.get('video_url'))
                download_url = _safe_url(video_data.get('download_url'))
                thumbnail_url = _safe_url(video_data.get('thumbnail_url'))
                # video_url is required; fallback if empty
                if not video_url:
                    vid = video_data.get('video_id', '')
                    if self.platform == Platform.FACEBOOK:
                        video_url = f"https://www.facebook.com/watch/?v={vid}" if vid else "https://www.facebook.com/"
                    else:
                        video_url = f"https://placeholder/{vid}" if vid else "https://placeholder"

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
        min_views: int = 0,
        min_comments: int = 0,
        filter_mode: str = "and"
    ) -> List[Dict[str, Any]]:
        """
        Filter video results based on criteria.
        
        Args:
            videos: List of video data
            keyword: The search keyword (for relevance check)
            min_likes: Minimum likes threshold
            min_views: Minimum views threshold
            min_comments: Minimum comments threshold
            filter_mode: "and" = all thresholds must pass; "or" = any threshold passes (for Instagram)
            
        Returns:
            Filtered list of videos
        """
        self.logger.info(
            f"Filtering {len(videos)} videos. "
            f"Criteria: min_likes={min_likes}, min_views={min_views}, min_comments={min_comments}, mode={filter_mode}"
        )
            
        filtered = []
        has_filter = min_likes > 0 or min_views > 0 or min_comments > 0
        
        for v in videos:
            likes = v.get('likes_count', 0) or 0
            views = v.get('views_count', 0) or 0
            comments = v.get('comments_count', 0) or 0
            
            if not has_filter:
                filtered.append(v)
                continue
            
            if filter_mode == "or":
                # Instagram: keep if ANY threshold is met
                if (likes >= min_likes) or (views >= min_views) or (comments >= min_comments):
                    filtered.append(v)
            else:
                # AND mode (TikTok etc): all thresholds must pass
                if likes < min_likes or views < min_views:
                    continue
                if min_comments > 0 and comments < min_comments:
                    continue
                filtered.append(v)
            
        self.logger.info(f"Filtered {len(videos)} videos to {len(filtered)}")
        return filtered
    
    def execute_search(
        self,
        keyword: str,
        min_likes: int = 0,
        min_views: int = 0,
        min_comments: int = 0,
        max_results: int = 20,
        page: int = 1,
        search_mode: str = "hashtag",
        use_cache: bool = True,
        save_to_db: bool = True,
        session_id: str = None
    ) -> Dict[str, Any]:
        """
        Execute a complete search operation with caching and error handling.
        """
        self.logger.info(f"execute_search: platform={self.platform}, keyword={keyword}, session_id={session_id}")
        start_time = time.time()
        page_size = max_results
        
        try:
            # Check cache first (only for page 1)
            # Variety logic: Nếu có cache, hãy sample ngẫu nhiên từ pool cache để mỗi lần search ra kết quả khác nhau
            if use_cache and page == 1:
                cached = self.check_cache(keyword, min_likes, min_views, max_results, search_mode)
                if cached and cached.raw_results:
                    pool = cached.raw_results
                    rng = random.Random(int(time.time() * 1000))
                    
                    # Nếu cache có pool lớn hơn yêu cầu, hãy sample
                    if len(pool) > max_results:
                        sampled = rng.sample(pool, min(max_results, len(pool)))
                        self.logger.info(f"Cache Variety Hit: sampled {max_results} from {len(pool)} cached results")
                        return {
                            'success': True,
                            'cached': True,
                            'results': sampled,
                            'count': len(sampled),
                            'execution_time': 0,
                            'search_id': cached.id,
                            'page': 1
                        }
                    # Nếu cache chỉ có vừa đủ hoặc ít hơn, trả về toàn bộ (shuffle cho vui)
                    else:
                        shuffled = list(pool)
                        rng.shuffle(shuffled)
                        return {
                            'success': True,
                            'cached': True,
                            'results': shuffled,
                            'count': len(shuffled),
                            'execution_time': 0,
                            'search_id': cached.id,
                            'page': 1
                        }
            
            # Create search history entry (page 1 only for DB tracking)
            search_history = self.create_search_history(
                keyword=keyword,
                min_likes=min_likes,
                min_views=min_views,
                max_results=max_results,
                search_mode=search_mode
            )
            
            try:
                # Variety: Fetch pool vừa đủ (max 35) để tiết kiệm token và có 5 video buffer cho Random Sample
                fetch_limit = min(max_results + 5, 35)
                
                self.logger.info(f"Searching {self.platform} for: {keyword} (page={page}, pool_limit={fetch_limit})")
                raw_results = self.search_videos(
                    keyword=keyword,
                    min_likes=min_likes,
                    min_views=min_views,
                    min_comments=min_comments,
                    max_results=fetch_limit,
                    search_mode=search_mode,
                    session_id=session_id
                )
                
                # Normalize results
                normalized_results = []
                for video in raw_results:
                    if not isinstance(video, dict):
                        continue
                    norm = self.normalize_video_data(video)
                    if norm and norm.get('video_id'):
                        normalized_results.append(norm)
                
                # Filter results
                filter_mode = "or" if self.platform in (Platform.INSTAGRAM, Platform.FACEBOOK) else "and"
                filtered_results = self.filter_results(
                    normalized_results,
                    keyword=keyword,
                    min_likes=min_likes,
                    min_views=min_views,
                    min_comments=min_comments,
                    filter_mode=filter_mode
                )
                
                # Fallback: nếu filter trả 0 nhưng có kết quả raw -> hiển thị tất cả
                filter_fallback = False
                if len(filtered_results) == 0 and len(normalized_results) > 0 and (min_likes > 0 or min_views > 0 or min_comments > 0):
                    filtered_results = normalized_results
                    filter_fallback = True
                
                # --- Variety logic: Sample 30 from the expanded pool (filtered_results) ---
                rng = random.Random(int(time.time() * 1000))
                
                # Pool toàn bộ bài hợp lệ
                full_pool = list(filtered_results)
                
                # Paginate logic:
                if page == 1:
                    # Nếu pool lớn hơn yêu cầu, chọn ngẫu nhiên max_results bài
                    if len(full_pool) > max_results:
                        page_results = rng.sample(full_pool, max_results)
                    else:
                        page_results = full_pool
                        rng.shuffle(page_results)
                else:
                    # Cho các trang sau (2, 3...), lấy theo offset truyền thống
                    start_idx = (page - 1) * page_size
                    end_idx = start_idx + page_size
                    page_results = full_pool[start_idx:end_idx]
                
                execution_time = time.time() - start_time
                
                # Save to database (Save PHẦN LỚN NHẤT CỦA POOL vào cache để lần sau random tiếp được)
                # Lưu tối đa 60 bài vào cache cho 1 keyword
                if save_to_db and page == 1:
                    cache_pool = full_pool[:60] if len(full_pool) > 60 else full_pool
                    self.save_videos(cache_pool, search_history)
                    search_history.mark_completed(cache_pool, execution_time)
                
                return {
                    'success': True,
                    'cached': False,
                    'results': page_results,
                    'count': len(page_results),
                    'execution_time': execution_time,
                    'search_id': search_history.id,
                    'page': page,
                    'has_more': len(full_pool) > (page * page_size),
                    'filter_fallback': filter_fallback
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
