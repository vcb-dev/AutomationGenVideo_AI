"""
Celery tasks for asynchronous operations.

This module defines background tasks for video scraping, channel monitoring,
and cache cleanup.
"""

import logging
from typing import Dict, Any
from celery import shared_task
from django.utils import timezone
from datetime import timedelta

from .models import TrackedChannel, SearchHistory, SearchStatus, Platform
from .services.apify_service import create_scraper

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name='video_management.search_videos',
    max_retries=3,
    default_retry_delay=60
)
def search_videos_task(
    self,
    platform: str,
    keyword: str,
    min_likes: int = 0,
    min_views: int = 0,
    min_comments: int = 0,
    max_results: int = 30,
    page: int = 1,
    use_cache: bool = True,
    search_type: str = 'posts',
    search_mode: str = 'hashtag',
    session_id: str = None
) -> Dict[str, Any]:
    """
    Asynchronous video search task.
    
    Args:
        platform: Platform to search
        keyword: Search keyword
        min_likes: Minimum likes filter
        min_views: Minimum views filter
        max_results: Maximum results
        use_cache: Whether to use cache
        search_type: Type of content (posts, reels)
        
    Returns:
        Search result dictionary
    """
    try:
        logger.info(
            f"[Task {self.request.id}] Starting search: "
            f"platform={platform}, type={search_type}, keyword={keyword}"
        )
        
        scraper = create_scraper(platform, search_type=search_type)
        result = scraper.execute_search(
            keyword=keyword,
            min_likes=min_likes,
            min_views=min_views,
            min_comments=min_comments,
            max_results=max_results,
            page=page,
            search_mode=search_mode,
            use_cache=use_cache,
            save_to_db=True,
            session_id=session_id
        )
        
        logger.info(
            f"[Task {self.request.id}] Search completed: "
            f"found {result['count']} videos in {result['execution_time']:.2f}s"
        )
        
        return result
        
    except Exception as e:
        logger.error(
            f"[Task {self.request.id}] Search failed: {str(e)}",
            exc_info=True
        )
        
        # Retry with exponential backoff
        try:
            raise self.retry(exc=e, countdown=2 ** self.request.retries)
        except self.MaxRetriesExceededError:
            return {
                'success': False,
                'error': str(e),
                'count': 0,
                'results': [],
                'execution_time': 0
            }


# DISABLED: Background task removed - channel checks now run synchronously via API
# TikHub integration removed - using Apify only


# DISABLED: Scheduled channel checks removed - use manual checks instead
# @shared_task(name='video_management.check_all_channels')
# def check_all_channels_task() -> Dict[str, Any]:
#     """
#     Check all active tracked channels.
#     
#     This task is scheduled to run periodically via Celery Beat.
#     
#     Returns:
#         Summary of checks performed
#     """
#     logger.info("Starting scheduled channel checks")
#     
#     # Get channels that should be checked
#     now = timezone.now()
#     channels = TrackedChannel.objects.filter(is_active=True)
#     
#     checked = 0
#     skipped = 0
#     
#     for channel in channels:
#         # Check if it's time to check this channel
#         if channel.last_checked_at:
#             next_check = channel.last_checked_at + timedelta(
#                 minutes=channel.check_interval_minutes
#             )
#             if now < next_check:
#                 skipped += 1
#                 continue
#         
#         # Start async check
#         check_channel_task.delay(channel.id)
#         checked += 1
#     
#     logger.info(
#         f"Scheduled checks completed: {checked} started, {skipped} skipped"
#     )
#     
#     return {
#         'success': True,
#         'checked': checked,
#         'skipped': skipped,
#         'total': channels.count()
#     }


@shared_task(name='video_management.cleanup_old_cache')
def cleanup_old_cache_task() -> Dict[str, Any]:
    """
    Clean up expired search cache entries.
    
    This task is scheduled to run daily via Celery Beat.
    
    Returns:
        Cleanup summary
    """
    logger.info("Starting cache cleanup")
    
    try:
        # Delete expired cache entries
        expired = SearchHistory.objects.filter(
            expires_at__lt=timezone.now(),
            status=SearchStatus.COMPLETED
        )
        count = expired.count()
        
        if count > 0:
            expired.delete()
            logger.info(f"Deleted {count} expired cache entries")
        else:
            logger.info("No expired cache entries to delete")
        
        # Also clean up very old failed searches (>30 days)
        old_failed = SearchHistory.objects.filter(
            status=SearchStatus.FAILED,
            created_at__lt=timezone.now() - timedelta(days=30)
        )
        failed_count = old_failed.count()
        
        if failed_count > 0:
            old_failed.delete()
            logger.info(f"Deleted {failed_count} old failed searches")
        
        return {
            'success': True,
            'expired_deleted': count,
            'failed_deleted': failed_count
        }
        
    except Exception as e:
        logger.error(f"Cache cleanup failed: {str(e)}", exc_info=True)
        return {
            'success': False,
            'error': str(e)
        }


@shared_task(name='video_management.update_video_stats')
def update_video_stats_task(video_ids: list = None) -> Dict[str, Any]:
    """
    Update statistics for videos (optional feature for refreshing data).
    
    Args:
        video_ids: List of video IDs to update (None = all recent)
        
    Returns:
        Update summary
    """
    # This is a placeholder for future enhancement
    # Could re-scrape videos to update their stats
    logger.info("Video stats update task (not yet implemented)")
    
    return {
        'success': True,
        'message': 'Feature not yet implemented'
    }
