"""
API views for search functionality.

This module provides REST API endpoints for searching videos across platforms.
"""

import logging
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from django.db.models import Count, Q, Sum
from celery.result import AsyncResult

from datetime import datetime, timedelta
from django.utils import timezone
from ..models import SearchHistory, ScrapedVideo, Platform
from ..serializers import (
    SearchRequestSerializer,
    SearchResultSerializer,
    TaskStatusSerializer,
    SearchHistorySerializer,
    VideoSerializer,
    UserVideosRequestSerializer,
)
from ..services.apify_service import create_scraper
from ..tasks import search_videos_task

logger = logging.getLogger(__name__)


class SearchView(APIView):
    """
    Search for videos across platforms.
    
    POST /api/search/
    
    Request body:
        {
            "platform": "tiktok",
            "keyword": "viral dance",
            "min_likes": 1000,
            "min_views": 10000,
            "max_results": 20,
            "use_cache": true,
            "async_mode": false
        }
    
    Response:
        {
            "success": true,
            "cached": false,
            "async_mode": false,
            "search_id": 123,
            "count": 15,
            "execution_time": 12.5,
            "results": [...]
        }
    """
    
    def post(self, request):
        """Handle search request."""
        # Validate request data
        serializer = SearchRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    'success': False,
                    'error': 'Invalid request data',
                    'details': serializer.errors
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        data = serializer.validated_data
        platform_str = data['platform']
        keyword = data['keyword']
        min_likes = data.get('min_likes', 0)
        min_views = data.get('min_views', 0)
        max_results = data.get('max_results', 20)
        use_cache = data.get('use_cache', True)
        async_mode = data.get('async_mode', False)
        search_type = data.get('search_type', 'posts')
        
        logger.info(
            f"Search request: platform={platform_str}, type={search_type}, keyword={keyword}, "
            f"min_likes={min_likes}, min_views={min_views}, "
            f"max_results={max_results}, async={async_mode}"
        )
        
        try:
            # Async mode - use Celery
            if async_mode:
                task = search_videos_task.delay(
                    platform=platform_str,
                    keyword=keyword,
                    min_likes=min_likes,
                    min_views=min_views,
                    max_results=max_results,
                    use_cache=use_cache,
                    search_type=search_type
                )
                
                return Response({
                    'success': True,
                    'async_mode': True,
                    'task_id': task.id,
                    'message': 'Search task started. Use /api/search/status/{task_id} to check progress.',
                    'status_url': f'/api/search/status/{task.id}'
                }, status=status.HTTP_202_ACCEPTED)
            
            # Sync mode - execute immediately
            scraper = create_scraper(platform_str, search_type=search_type)
            result = scraper.execute_search(
                keyword=keyword,
                min_likes=min_likes,
                min_views=min_views,
                max_results=max_results,
                use_cache=use_cache,
                save_to_db=True
            )
            
            if not result['success']:
                return Response(
                    {
                        'success': False,
                        'error': result.get('error', 'Search failed'),
                        'execution_time': result.get('execution_time', 0)
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Serialize results
            # Safely fetch videos that were saved to DB
            video_ids = [v['video_id'] for v in result['results'] if 'video_id' in v]
            
            # Preserve original order from platform (don't sort by likes/views)
            videos = ScrapedVideo.objects.filter(video_id__in=video_ids)
            
            # Maintain original order from results
            video_id_to_video = {v.video_id: v for v in videos}
            ordered_videos = []
            for video_id in video_ids:
                if video_id in video_id_to_video:
                    ordered_videos.append(video_id_to_video[video_id])
            
            videos_serializer = VideoSerializer(
                ordered_videos,
                many=True
            )
            
            response_data = {
                'success': True,
                'cached': result.get('cached', False),
                'async_mode': False,
                'search_id': result.get('search_id'),
                'count': result['count'],
                'execution_time': result['execution_time'],
                'results': videos_serializer.data
            }
            
            return Response(response_data, status=status.HTTP_200_OK)
            
        except ValueError as e:
            logger.error(f"Invalid platform: {str(e)}")
            return Response(
                {
                    'success': False,
                    'error': str(e)
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"Search error: {str(e)}", exc_info=True)
            return Response(
                {
                    'success': False,
                    'error': f'Internal server error: {str(e)}'
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class SearchStatusView(APIView):
    """
    Check status of async search task.
    
    GET /api/search/status/{task_id}/
    
    Response:
        {
            "task_id": "abc123",
            "status": "SUCCESS",
            "ready": true,
            "successful": true,
            "result": {...}
        }
    """
    
    def get(self, request, task_id):
        """Get task status."""
        try:
            task = AsyncResult(task_id)
            
            response_data = {
                'task_id': task_id,
                'status': task.status,
                'ready': task.ready(),
            }
            
            if task.ready():
                response_data['successful'] = task.successful()
                
                if task.successful():
                    result = task.result
                    
                    # If result contains video IDs, fetch and serialize them
                    if result.get('success') and result.get('results'):
                        try:
                            video_ids = [v.get('video_id') for v in result['results'] if v.get('video_id')]
                            videos = ScrapedVideo.objects.filter(video_id__in=video_ids)
                            videos_serializer = VideoSerializer(videos, many=True)
                            result['results'] = videos_serializer.data
                        except Exception as e:
                            logger.warning(f"Failed to serialize videos: {str(e)}")
                    
                    response_data['result'] = result
                else:
                    response_data['error'] = str(task.result)
                    response_data['traceback'] = task.traceback
            
            return Response(response_data, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error checking task status: {str(e)}", exc_info=True)
            return Response(
                {
                    'success': False,
                    'error': f'Failed to check task status: {str(e)}'
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class SearchHistoryView(APIView):
    """
    Get search history.
    
    GET /api/search/history/
    
    Query params:
        - platform: Filter by platform
        - limit: Limit results (default: 50)
    """
    
    def get(self, request):
        """Get search history."""
        try:
            platform = request.query_params.get('platform')
            limit = int(request.query_params.get('limit', 50))
            
            queryset = SearchHistory.objects.all()
            
            if platform:
                try:
                    platform_enum = Platform[platform.upper()]
                    queryset = queryset.filter(platform=platform_enum)
                except KeyError:
                    return Response(
                        {'error': f'Invalid platform: {platform}'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            searches = queryset.order_by('-created_at')[:limit]
            serializer = SearchHistorySerializer(searches, many=True)
            
            return Response({
                'count': len(serializer.data),
                'results': serializer.data
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error fetching search history: {str(e)}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class UserVideosView(APIView):
    """
    Get videos from a specific user.
    
    POST /api/search/user-videos/
    
    Request body:
        {
            "platform": "tiktok",
            "username": "username",
            "max_results": 9999  // Default: unlimited (up to Apify's limit)
        }
    """
    
    def post(self, request):
        """Handle user videos request."""
        serializer = UserVideosRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    'success': False,
                    'error': 'Invalid request data',
                    'details': serializer.errors
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        data = serializer.validated_data
        platform_str = data['platform']
        username = data['username']
        # Use provided max_results or default from serializer (9999).
        # We need to fetch ALL videos to get accurate Total Views/Engagement stats.
        max_results = data.get('max_results')
        
        logger.info(f"Processing request for {username} on {platform_str}")

        try:
            # Using Apify only - TikHub removed as per user request
            logger.info(f"⚡ Using Apify to fetch videos for @{username} on {platform_str}")
            scraper = create_scraper(platform_str)
            raw_results = scraper.get_user_videos(username, max_results=max_results)
            
            normalized = []
            for v in raw_results:
                norm = scraper.normalize_video_data(v)
                if norm:
                    normalized.append(norm)

            # Extract profile data from Apify authorMeta
            profile_data = None
            if raw_results:
                first_item = raw_results[0]
                # authorMeta is sometimes at root, sometimes nested in raw_data
                author_meta = first_item.get('authorMeta') or first_item.get('raw_data', {}).get('authorMeta', {})
                
                # Robust extraction with multiple fallbacks
                username = author_meta.get('name') or author_meta.get('uniqueId') or username
                display_name = author_meta.get('nickName') or author_meta.get('nickname') or username
                
                # Avatar strategies
                avatar_url = (
                    author_meta.get('avatarLarger') or 
                    author_meta.get('avatarMedium') or 
                    author_meta.get('avatarThumb') or 
                    author_meta.get('avatar') or 
                    ''
                )
                
                # Stats strategies
                follower_count = int(author_meta.get('fans') or author_meta.get('followerCount') or 0)
                total_likes = int(author_meta.get('heart') or author_meta.get('heartCount') or author_meta.get('diggCount') or 0)
                total_videos = int(author_meta.get('video') or author_meta.get('videoCount') or len(normalized))
                
                # Calculate aggregated stats from fetched videos
                fetched_views = sum(v.get('views_count', 0) for v in normalized)
                fetched_likes = sum(v.get('likes_count', 0) for v in normalized)
                fetched_comments = sum(v.get('comments_count', 0) for v in normalized)
                fetched_shares = sum(v.get('shares_count', 0) for v in normalized)
                
                # Engagement Rate Calculation
                # Formula 1: (Likes + Comments + Shares) / Views * 100 (Engagement per View) -> Good for content performance
                # Formula 2: (Total Likes / Followers) * 100 -> Good for account popularity
                # We'll use Formula 1 based on the fetched batch as it's more dynamic, OR allow Formula 2 if Views are 0.
                
                engagement_rate = 0.0
                if fetched_views > 0:
                    engagement_rate = ((fetched_likes + fetched_comments + fetched_shares) / fetched_views) * 100
                elif follower_count > 0:
                    # Fallback to (Total Likes / Followers) if no view data (which is unlikely if we fetched videos)
                    engagement_rate = (total_likes / follower_count) * 100

                # Extract profile info from authorMeta
                profile_data = {
                    'username': username,
                    'display_name': display_name,
                    'avatar_url': avatar_url,
                    'follower_count': follower_count,
                    'total_likes': total_likes,
                    'total_videos': total_videos,
                    'total_views': fetched_views, # Add calculated views
                    'engagement_rate': round(engagement_rate, 2), # Add calculated rate
                    'platform': platform_str
                }
                
                logger.info(f"📊 Profile data extracted: {profile_data['follower_count']:,} followers, {profile_data['engagement_rate']}% engagement")

            # Save videos
            ordered_videos = []
            if normalized:
                try:
                    saved_videos = scraper.save_videos(normalized)
                    # Map back to preserve order
                    video_id_to_video = {v.video_id: v for v in saved_videos}
                    for norm in normalized:
                        vid = norm.get('video_id')
                        if vid in video_id_to_video:
                            ordered_videos.append(video_id_to_video[vid])
                except Exception as e:
                    logger.error(f"Error saving videos: {e}")
                    # If save fails, just return normalized data for now
                    ordered_videos = normalized

            videos_serializer = VideoSerializer(ordered_videos, many=True)
            
            response_data = {
                'success': True,
                'platform': platform_str,
                'username': username,
                'count': len(ordered_videos),
                'results': videos_serializer.data,
                'profile': profile_data  # Ensure profile is always in root if available
            }
                
            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"User videos error: {str(e)}", exc_info=True)
            return Response(
                {
                    'success': False,
                    'error': str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class VideosByChannelView(APIView):
    """
    Get videos by channel username (search in DB).
    GET /api/videos/by-channel/?platform=TIKTOK&username=...
    """
    def get(self, request):
        try:
            platform_str = request.query_params.get('platform')
            username = request.query_params.get('username')
            limit = int(request.query_params.get('limit', 20))
            if limit > 1000: # Safe cap if not explicitly unlimited 
                 limit = 1000
            
            # Allow unlimited if specifically requested with high limit for analytics
            if request.query_params.get('limit') == '10000':
                 limit = 10000

            sort_by = request.query_params.get('sort_by', 'views') # views, likes, date
            order = request.query_params.get('order', 'desc')
            period = request.query_params.get('period')

            if not platform_str or not username:
                return Response(
                    {'success': False, 'error': 'Platform and username are required'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            try:
                platform_enum = Platform[platform_str.upper()]
            except KeyError:
                return Response(
                    {'success': False, 'error': f'Invalid platform: {platform_str}'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Base query
            videos_query = ScrapedVideo.objects.filter(
                platform=platform_enum,
                author_username=username
            )
            
            # Period Filtering
            if period:
                period_lower = period.lower().strip()
                logger.info(f"Filtering videos by period: {period_lower} for {username}")
                
                # Default Timezone Offset to UTC+7 (Vietnam)
                # Ideally this comes from user profile/request, but defaulting for this user context.
                TZ_OFFSET = 7 
                
                now_utc = timezone.now()
                now_local = now_utc + timedelta(hours=TZ_OFFSET)
                
                if period_lower == 'yesterday':
                    yesterday_local = now_local - timedelta(days=1)
                    
                    # Local Day Boundaries
                    start_local = yesterday_local.replace(hour=0, minute=0, second=0, microsecond=0)
                    end_local = yesterday_local.replace(hour=23, minute=59, second=59, microsecond=999999)
                    
                    # Convert back to UTC for Filtering
                    start_utc = start_local - timedelta(hours=TZ_OFFSET)
                    end_utc = end_local - timedelta(hours=TZ_OFFSET)
                    
                    videos_query = videos_query.filter(
                        published_at__gte=start_utc,
                        published_at__lte=end_utc
                    )
                
                elif period_lower == 'week':
                    # Start of week (Monday) Local
                    start_of_week_local = now_local - timedelta(days=now_local.weekday())
                    start_of_week_local = start_of_week_local.replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    start_utc = start_of_week_local - timedelta(hours=TZ_OFFSET)
                    videos_query = videos_query.filter(published_at__gte=start_utc)
                
                elif period_lower == 'month':
                    # Start of month Local
                    start_of_month_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    
                    start_utc = start_of_month_local - timedelta(hours=TZ_OFFSET)
                    videos_query = videos_query.filter(published_at__gte=start_utc)
                    
                elif period_lower == 'all':
                    pass 
                    
                else:
                    logger.warning(f"Unknown period '{period}' requested. Returning empty list.")
                    videos_query = videos_query.none()

            # Sorting
            if sort_by == 'date' or sort_by == 'published_at':
                sort_field = 'published_at'
            elif sort_by == 'likes':
                sort_field = 'likes_count'
            else:
                sort_field = 'views_count'
            
            if order == 'desc':
                sort_field = f'-{sort_field}'
            
            # Execute query with limit
            videos = videos_query.order_by(sort_field)[:limit]
            serializer = VideoSerializer(videos, many=True)
            
            # Get aggregates for ALL videos of this channel (after period filter, before limit)
            all_videos_for_stats = videos_query
            total_videos = all_videos_for_stats.count()
            
            aggs = all_videos_for_stats.aggregate(
                total_views=Sum('views_count'),
                total_likes=Sum('likes_count'),
                total_comments=Sum('comments_count'),
                total_shares=Sum('shares_count')
            )

            return Response({
                'success': True,
                'count': len(videos),
                'total_found': total_videos,
                'results': serializer.data,
                'aggregate_stats': {
                    'total_videos': total_videos,
                    'total_views': aggs['total_views'] or 0,
                    'total_likes': aggs['total_likes'] or 0,
                    'total_comments': aggs['total_comments'] or 0,
                    'total_shares': aggs['total_shares'] or 0,
                },
                'debug_info': {
                    'period': period,
                    'period_interpreted': period_lower if period else None,
                    'start_date': str(start_date) if 'start_date' in locals() else None,
                    'end_date': str(end_date) if 'end_date' in locals() else None,
                }
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error getting videos by channel: {str(e)}", exc_info=True)
            return Response(
                {'success': False, 'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
