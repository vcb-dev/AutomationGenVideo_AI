from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.conf import settings
from django.core.cache import cache
from celery.result import AsyncResult
import logging
import traceback
import time
import asyncio

from ..models import SearchCache
from ..serializers import DouyinSearchSerializer

logger = logging.getLogger(__name__)

@method_decorator(csrf_exempt, name='dispatch')
class SearchView(APIView):
    """
    Search View with support for:
    1. Cache check
    2. Direct API call (Tikhub)
    3. Scraper fallback (Playwright)
    """
    def post(self, request):
        try:
            serializer = DouyinSearchSerializer(data=request.data)
            if not serializer.is_valid():
                logger.warning(f"Invalid serializer data: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
            keyword = serializer.validated_data['keyword']
            sort_by = serializer.validated_data.get('sort_by', 'likes')
            min_likes = serializer.validated_data.get('min_likes', 0) or 0
            min_views = serializer.validated_data.get('min_views', 0) or 0

            if not keyword or not keyword.strip():
                return Response(
                    {"error": "Keyword is required"}, 
                    status=status.HTTP_400_BAD_REQUEST
                )

            logger.info(f"Search request received: keyword='{keyword}', min_likes={min_likes}, min_views={min_views}")

            # 1. Check Cache
            cache_key = f"adv_search_{keyword}_{min_likes}_{min_views}"
            cached_videos = cache.get(cache_key)
            
            if cached_videos:
                logger.info(f"Cache hit for {cache_key}")
                if cached_videos and sort_by in ['likes', 'like_count']:
                    cached_videos.sort(key=lambda x: x.get('likes', 0), reverse=True)
                elif cached_videos and sort_by in ['views', 'view_count']:
                    cached_videos.sort(key=lambda x: x.get('views', 0), reverse=True)
                return Response(cached_videos, status=status.HTTP_200_OK)

            # 2. Direct API Search
            logger.info(f"Searching videos directly for keyword: '{keyword}'")
            videos = []
            cursor = 0
            
            try:
                from ..services.rapidapi_service import TikhubService
                api_service = TikhubService()
                
                start_time = time.time()
                search_result = api_service.search_videos(
                    keyword=keyword,
                    min_likes=min_likes,
                    min_views=min_views,
                    target_count=20
                )
                
                videos = search_result.get('videos', []) if isinstance(search_result, dict) else []
                cursor = search_result.get('cursor', 0) if isinstance(search_result, dict) else 0
                
                elapsed_time = time.time() - start_time
                if videos:
                    logger.info(f"Tikhub API found {len(videos)} videos in {elapsed_time:.2f}s")
                else:
                    logger.warning(f"Tikhub API returned no results after {elapsed_time:.2f}s")
            except Exception as api_error:
                logger.error(f"Tikhub API search failed: {api_error}", exc_info=True)
                videos = []

            # 3. Scraper Fallback
            if not videos:
                use_scraper_fallback = getattr(settings, 'USE_SCRAPER_FALLBACK', False)
                if use_scraper_fallback:
                    logger.info("Trying Playwright scraper fallback...")
                    try:
                        from ..services.scraper_service import DouyinScraper
                        
                        start_time = time.time()
                        scraper = DouyinScraper(headless=True)
                        scraper_results = asyncio.run(scraper.search_videos(
                            keyword=keyword,
                            min_likes=min_likes,
                            min_views=min_views,
                            target_count=20
                        ))
                        
                        elapsed_time = time.time() - start_time
                        if scraper_results:
                            videos = scraper_results
                            logger.info(f"Scraper found {len(videos)} videos in {elapsed_time:.2f}s")
                    except Exception as scraper_error:
                        logger.error(f"Scraper fallback failed: {scraper_error}", exc_info=True)
                else:
                    logger.info("Scraper fallback is disabled")

            # 4. Result Processing
            if videos:
                # Apply sorting
                if sort_by in ['likes', 'like_count']:
                    videos.sort(key=lambda x: x.get('likes', 0), reverse=True)
                elif sort_by in ['views', 'view_count']:
                    videos.sort(key=lambda x: x.get('views', 0), reverse=True)
                
                # Validation & Formatting
                validated_videos = []
                for video in videos:
                    if not isinstance(video, dict): continue
                    
                    validated_video = {
                        'id': str(video.get('id', '')),
                        'caption': str(video.get('caption', 'No title')),
                        'thumbnail': str(video.get('thumbnail', 'https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=120&h=80&fit=crop')),
                        'likes': int(video.get('likes', 0)),
                        'views': int(video.get('views', 0)),
                        'channelName': str(video.get('channelName', 'Unknown Channel')),
                        'url': str(video.get('url', '')),
                        'status': str(video.get('status', 'completed')),
                        'publishedAt': str(video.get('publishedAt', timezone.now().isoformat()))
                    }
                    validated_videos.append(validated_video)
                
                # Cache & Response
                cache.set(cache_key, validated_videos, timeout=1800)
                
                return Response({
                    'videos': validated_videos,
                    'cursor': cursor,
                    'count': len(validated_videos)
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    "error": "No videos found matching your search criteria.",
                    "suggestions": [
                        "Try a different keyword",
                        f"Reduce min_likes (current: {min_likes})",
                        f"Reduce min_views (current: {min_views})"
                    ]
                }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Search view error: {e}", exc_info=True)
            return Response(
                {"error": f"An error occurred: {str(e)}"}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class SearchStatusView(APIView):
    """Check status of Celery task"""
    def get(self, request, task_id):
        try:
            task_result = AsyncResult(task_id)
            response = {'task_id': task_id}
            
            if task_result.state == 'PENDING':
                response.update({'status': 'PENDING', 'message': 'Task is waiting'})
            elif task_result.state == 'PROGRESS':
                response.update({
                    'status': 'PROGRESS', 
                    'message': (task_result.info or {}).get('status', 'Processing...'),
                    'progress': task_result.info
                })
            elif task_result.state == 'SUCCESS':
                result = task_result.info
                if result.get('status') == 'SUCCESS':
                    response.update({
                        'status': 'SUCCESS', 
                        'videos': result.get('videos', []),
                        'count': len(result.get('videos', []))
                    })
                else:
                    response.update({
                        'status': 'FAILURE', 
                        'error': result.get('error', 'Unknown error')
                    })
            else:
                response.update({
                    'status': 'FAILURE', 
                    'error': str(task_result.info) if task_result.info else 'Task failed'
                })
            
            return Response(response, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Search status error: {e}", exc_info=True)
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class TestSearchView(APIView):
    """Test endpoint for debugging"""
    def get(self, request):
        keyword = request.GET.get('keyword', 'test')
        try:
            from ..services.rapidapi_service import TikhubService
            api_service = TikhubService()
            videos = api_service.search_videos(keyword=keyword, target_count=5)
            
            return Response({
                'keyword': keyword,
                'tikhub_results': len(videos) if videos else 0,
                'tikhub_videos': videos[:2] if videos else [],
                'api_host': api_service.api_host,
                'api_key_configured': bool(api_service.api_key)
            })
        except Exception as e:
            return Response(
                {'error': str(e), 'traceback': traceback.format_exc()}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
