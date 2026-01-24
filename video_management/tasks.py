from celery import shared_task
from celery.result import AsyncResult
from .models import TrackedChannel, ReportedVideo
from .services.douyin_client import DouyinClient
from .services.telegram_utils import TelegramService
from .services.rapidapi_service import TikhubService
from django.core.cache import cache
import logging
import asyncio

logger = logging.getLogger(__name__)

@shared_task(bind=True, name='video_management.search_videos_task')
def search_videos_task(self, keyword, min_likes=0, min_views=0, sort_by='likes', target_count=30):
    """
    Celery task để search videos từ TikTok/Douyin sử dụng Tikhub API (chạy background)
    
    Flow:
    1. Django Backend trigger task
    2. Celery Task (ẩn - background)
    3. Tikhub TikTok API
    4. https://api.tikhub.io/api/v1/douyin/web/
    5. Parse JSON data
    6. Filter video (min_likes, min_views)
    7. Return JSON và cache
    """
    try:
        # Update task state
        self.update_state(state='PROGRESS', meta={'status': 'Starting Tikhub API search...'})
        
        logger.info(f"Celery task: Starting Tikhub API search for keyword: {keyword}, min_likes: {min_likes}, min_views: {min_views}")
        
        # Tạo cache key
        cache_key = f"adv_search_{keyword}_{min_likes}_{min_views}"
        
        # Kiểm tra cache trước
        cached_result = cache.get(cache_key)
        if cached_result:
            logger.info(f"Cache hit for {cache_key}")
            return {
                'status': 'SUCCESS',
                'videos': cached_result,
                'count': len(cached_result)
            }
        
        # Sử dụng Tikhub Service
        self.update_state(state='PROGRESS', meta={'status': 'Calling Tikhub API...'})
        
        api_service = TikhubService()
        formatted_videos = api_service.search_videos(
            keyword=keyword,
            min_likes=min_likes,
            min_views=min_views,
            target_count=target_count
        )
        
        # Validate results
        if formatted_videos is None:
            formatted_videos = []
        
        if not isinstance(formatted_videos, list):
            logger.warning(f"Tikhub API returned invalid format: {type(formatted_videos)}, falling back to scraper")
            formatted_videos = []
        
        # Nếu Tikhub API không trả về kết quả, fallback sang scraper
        if not formatted_videos or len(formatted_videos) == 0:
            logger.info("Tikhub API returned no results, falling back to Playwright scraper...")
            self.update_state(state='PROGRESS', meta={'status': 'Tikhub API returned no results, using Playwright scraper...'})
            
            try:
                from .services.scraper_service import DouyinScraper
                scraper = DouyinScraper(headless=True)
                scraper_results = asyncio.run(scraper.search_videos(
                    keyword=keyword,
                    min_likes=min_likes,
                    min_views=min_views,
                    target_count=target_count
                ))
                
                if scraper_results:
                    formatted_videos = scraper_results
                    logger.info(f"Scraper found {len(formatted_videos)} videos")
            except Exception as scraper_error:
                logger.error(f"Scraper fallback failed: {scraper_error}", exc_info=True)
                # Tiếp tục với formatted_videos = [] nếu scraper cũng fail
        
        # Apply sorting
        if formatted_videos:
            if sort_by in ['likes', 'like_count']:
                formatted_videos.sort(key=lambda x: x.get('likes', 0), reverse=True)
            elif sort_by in ['views', 'view_count']:
                formatted_videos.sort(key=lambda x: x.get('views', 0), reverse=True)
        
        # Cache results
        if formatted_videos:
            cache.set(cache_key, formatted_videos, timeout=1800)  # 30 minutes
        
        logger.info(f"Task completed. Found {len(formatted_videos)} videos")
        
        return {
            'status': 'SUCCESS',
            'videos': formatted_videos,
            'count': len(formatted_videos)
        }
        
    except Exception as e:
        error_msg = f"Tikhub API error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {
            'status': 'FAILURE',
            'error': error_msg
        }

@shared_task
def scan_tracked_channels():
    """
    Periodic task to scan all active TrackedChannel entries.
    If a new video meets the likes threshold and hasn't been reported,
    trigger a Telegram alert.
    """
    active_channels = TrackedChannel.objects.filter(is_active=True)
    client = DouyinClient()
    
    for channel in active_channels:
        # Fetch latest videos from Douyin API for this channel
        # Note: In a real celery task, we'd use a sync wrapper for the async client
        # or use a sync httpx client. Simplified here with asyncio.run.
        channel_data = asyncio.run(client.get_channel_videos(channel.channel_id))
        
        if not channel_data:
            continue
            
        videos = channel_data.get('list', [])
        for video in videos:
            video_id = video.get('video_id')
            like_count = video.get('like_count', 0)
            
            # Check Threshold & Duplicate
            if like_count >= channel.threshold_likes:
                if not ReportedVideo.objects.filter(video_id=video_id).exists():
                    # Trigger Telegram Notification
                    video_url = video.get('share_url', f"https://www.douyin.com/video/{video_id}")
                    asyncio.run(TelegramService.notify_hot_video(
                        channel_name=channel.name,
                        like_count=like_count,
                        video_url=video_url
                    ))
                    
                    # Record as reported
                    ReportedVideo.objects.create(
                        video_id=video_id,
                        channel=channel,
                        likes_at_report=like_count
                    )

@shared_task
def cleanup_old_cache():
    """Optional: Cleanup expired search cache entries."""
    from .models import SearchCache
    from django.utils import timezone
    SearchCache.objects.filter(expires_at__lt=timezone.now()).delete()
