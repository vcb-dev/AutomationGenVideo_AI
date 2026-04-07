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

from ..models import SearchHistory, ScrapedVideo, Platform, FacebookPageCache

from ..serializers import (

    SearchRequestSerializer,

    SearchResultSerializer,

    TaskStatusSerializer,

    SearchHistorySerializer,

    VideoSerializer,

    UserVideosRequestSerializer,

)

from ..services.apify_service import create_scraper, fetch_tiktok_user_profile
from ..services.base_scraper import ScraperException

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
        min_comments = data.get('min_comments', 0)
        page = data.get('page', 1)
        max_results = data.get('max_results', 30)
        search_mode = data.get('search_mode', 'hashtag')

        use_cache = data.get('use_cache', True)
        async_mode = data.get('async_mode', False)
        search_type = data.get('search_type', 'posts')
        session_id = data.get('session_id', None)
        
        # Force fresh scraping for Instagram to get latest posts
        if platform_str.lower() == 'instagram':
            use_cache = False
            logger.info("Instagram search: Forcing fresh scrape (use_cache=False)")
        
        logger.info(
            f"Search request: platform={platform_str}, type={search_type}, mode={search_mode}, keyword={keyword}, "
            f"min_likes={min_likes}, min_views={min_views}, min_comments={min_comments}, "
            f"page={page}, max_results={max_results}, async={async_mode}, use_cache={use_cache}, session_id={session_id}"
        )
        
        try:
            # Async mode - use Celery
            if async_mode:
                task = search_videos_task.delay(
                    platform=platform_str,
                    keyword=keyword,
                    min_likes=min_likes,
                    min_views=min_views,
                    min_comments=min_comments,
                    max_results=max_results,
                    page=page,
                    use_cache=use_cache,
                    search_type=search_type,
                    search_mode=search_mode,
                    session_id=session_id
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
                min_comments=min_comments,
                max_results=max_results,
                page=page,
                search_mode=search_mode,
                use_cache=use_cache,
                save_to_db=True,
                session_id=session_id
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

            

            # Return results directly (Bypass DB since save_to_db=False)
            results_data = result['results']
            
            # Ensure JSON serializable (convert datetimes)
            for item in results_data:
                if isinstance(item.get('published_at'), (datetime,)):
                    item['published_at'] = item['published_at'].isoformat()
            
            # Gắn DB id (integer primary key) vào mỗi result để FE dùng cho GenerateContentButton
            # save_to_db=True đã lưu ở execute_search (page=1), nên có thể query DB theo video_id
            try:
                video_ids = [item.get('video_id') for item in results_data if item.get('video_id')]
                if video_ids:
                    db_map = {
                        v.video_id: v.id
                        for v in ScrapedVideo.objects.filter(video_id__in=video_ids).only('id', 'video_id')
                    }
                    for item in results_data:
                        vid = item.get('video_id', '')
                        if vid in db_map:
                            item['id'] = db_map[vid]
                        elif not item.get('id'):
                            item['id'] = 0
            except Exception as _e:
                logger.warning(f"Could not enrich results with DB id: {_e}")

            response_data = {
                'success': True,
                'cached': result.get('cached', False),
                'async_mode': False,
                'search_id': result.get('search_id'),
                'count': result['count'],
                'execution_time': result['execution_time'],
                'results': results_data,
                'page': result.get('page', 1),
                'has_more': result.get('has_more', False),
                'filter_fallback': result.get('filter_fallback', False)
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
        start_date = data.get('start_date') or data.get('until_date')  # Backward compatibility
        end_date = data.get('end_date')
        # URL đầy đủ từ link_channel trong bảng Channel (VD: profile.php?id=...)
        channel_url = request.data.get('channel_url', '').strip() or None
        
        logger.info(f"Processing request for {username} on {platform_str} (start={start_date}, end={end_date})")
        if channel_url:
            logger.info(f"🔗 channel_url provided: {channel_url}")

        try:
            # --- FACEBOOK: Try Graph API first, fallback to Apify (HYBRID) ---
            if platform_str.upper() == 'FACEBOOK':
                page_info = {}
                raw_results = []
                use_graph_api = False
                
                # Check for force_refresh flag
                force_refresh = request.data.get('force_refresh', False)
                logger.info(f"🔍 FORCE_REFRESH FLAG: {force_refresh} (type: {type(force_refresh)})")
                logger.info(f"📦 Request data: {request.data}")
                
                # SMART CACHING STRATEGY
                # Step 1: Check DB coverage
                db_videos = []
                should_fetch_new = force_refresh
                fetch_strategy = "full"  # full, incremental, or cache_only
                
                logger.info(f"💾 Checking DB for @{username} on {platform_str}...")
                db_query = ScrapedVideo.objects.filter(
                    platform=Platform[platform_str.upper()],
                    author_username=username
                )
                
                # Parse date filters
                start_dt_db = None
                end_dt_db = None
                
                if start_date:
                    try:
                        from datetime import datetime as dt
                        start_dt_db = dt.strptime(start_date, '%Y-%m-%d')
                        if timezone.is_aware(timezone.now()):
                            start_dt_db = timezone.make_aware(start_dt_db)
                        logger.info(f"📅 DB Filter Start: {start_dt_db}")
                        db_query = db_query.filter(published_at__gte=start_dt_db)
                    except Exception as e:
                        logger.error(f"❌ Date Filter Error (Start): {str(e)}")
                    
                if end_date:
                    try:
                        from datetime import datetime as dt
                        end_dt_db = dt.strptime(end_date, '%Y-%m-%d')
                        end_dt_db = end_dt_db.replace(hour=23, minute=59, second=59)
                        if timezone.is_aware(timezone.now()):
                            end_dt_db = timezone.make_aware(end_dt_db)
                        logger.info(f"📅 DB Filter End: {end_dt_db}")
                        db_query = db_query.filter(published_at__lte=end_dt_db)
                    except Exception as e:
                        logger.error(f"❌ Date Filter Error (End): {str(e)}")
                
                # Execute query
                raw_db_videos = list(db_query.order_by('-published_at'))
                
                # Deduplicate
                seen_ids = set()
                db_videos = []
                for vid in raw_db_videos:
                    clean_id = vid.video_id
                    if '_' in clean_id:
                        clean_id = clean_id.split('_')[-1]
                    if clean_id not in seen_ids:
                        seen_ids.add(clean_id)
                        seen_ids.add(vid.video_id)
                        db_videos.append(vid)
                
                logger.info(f"✅ Found {len(db_videos)} videos in DB (after filtering & deduping).")
                
                # SMART DECISION: Analyze coverage
                # BUT: If force_refresh is True, ALWAYS fetch new data
                if force_refresh:
                    fetch_strategy = "full"
                    logger.info(f"🔄 Force refresh requested - bypassing cache")
                elif db_videos:
                    # Check data freshness
                    latest_db_post = db_videos[0].published_at if db_videos else None
                    now = timezone.now()
                    
                    if latest_db_post:
                        age_hours = (now - latest_db_post).total_seconds() / 3600
                        logger.info(f"📊 Latest DB post age: {age_hours:.1f} hours")
                        
                        # STRATEGY DECISION
                        if age_hours <= 1:
                            # Data is fresh (< 1 hour old)
                            fetch_strategy = "cache_only"
                            logger.info(f"✅ Using cache (data is fresh)")
                        elif age_hours <= 24:
                            # Data is recent but might have new posts
                            if end_date and end_dt_db:
                                # User filtering by date range
                                gap_days = (end_dt_db - latest_db_post).days
                                if gap_days <= 1:
                                    fetch_strategy = "cache_only"
                                    logger.info(f"✅ Using cache (coverage is good)")
                                else:
                                    fetch_strategy = "incremental"
                                    logger.info(f"⚡ Incremental fetch (gap: {gap_days} days)")
                            else:
                                fetch_strategy = "incremental"
                                logger.info(f"⚡ Incremental fetch (update recent posts)")
                        else:
                            # Data is old (> 24 hours)
                            fetch_strategy = "full"
                            logger.info(f"🔄 Full fetch (data is stale)")
                    else:
                        fetch_strategy = "full"
                else:
                    fetch_strategy = "full"
                
                # Execute strategy
                if fetch_strategy == "cache_only" and db_videos:
                    # Return cached data immediately
                    logger.info(f"🚀 Returning {len(db_videos)} cached videos (instant)")
                    normalized = []
                    for vid in db_videos:
                        norm_item = {
                            'video_id': vid.video_id,
                            'title': vid.title,
                            'description': vid.description,
                            'video_url': vid.video_url,
                            'thumbnail_url': vid.thumbnail_url,
                            'likes_count': vid.likes_count,
                            'comments_count': vid.comments_count,
                            'shares_count': vid.shares_count,
                            'views_count': vid.views_count,
                            'published_at': vid.published_at,
                            'timestamp': int(vid.published_at.timestamp()) if vid.published_at else 0,
                            'author_name': vid.author_name,
                            'author_username': vid.author_username,
                            'platform': platform_str.lower(),
                            'raw_data': vid.raw_data
                        }
                        normalized.append(norm_item)
                    
                    # Extract profile from latest video
                    first_vid = db_videos[0]
                    avatar_url = first_vid.thumbnail_url
                    if first_vid.raw_data:
                        raw = first_vid.raw_data
                        if 'owner' in raw and isinstance(raw['owner'], dict):
                            avatar_url = raw['owner'].get('profile_picture_url') or avatar_url
                        elif 'from' in raw and isinstance(raw['from'], dict):
                            avatar_url = raw['from'].get('picture', {}).get('data', {}).get('url') or avatar_url
                    
                    profile_data = {
                        'username': username,
                        'display_name': first_vid.author_name or username,
                        'avatar_url': avatar_url,
                        'followers': 0,
                        'likes': 0
                    }
                    
                    return Response({
                        'results': normalized,
                        'profile': profile_data,
                        'total': len(normalized),
                        'source': 'database_cache',
                        'cache_age_hours': age_hours if latest_db_post else 0
                    })
                
                # Need to fetch new data
                logger.info(f"🔄 Fetching from external source (strategy: {fetch_strategy})...")
                
                # Calculate smart limit for incremental fetch
                if fetch_strategy == "incremental" and db_videos:
                    # Only fetch recent posts (last 3 days worth)
                    smart_limit = 60  # ~20 posts/day * 3 days
                    logger.info(f"⚡ Incremental fetch: limit={smart_limit}")
                elif start_date and end_date:
                    # User filtering by date range
                    # With 14-day limit, fetch time is acceptable (40-60s)
                    # Cap at reasonable limit to avoid excessive fetching
                    from datetime import datetime as dt
                    try:
                        start_dt = dt.strptime(start_date, '%Y-%m-%d')
                        end_dt = dt.strptime(end_date, '%Y-%m-%d')
                        
                        # FIX: Calculate limit based on distance from NOW into past.
                        # Apify crawls backwards (Newest -> Oldest).
                        # If filtering for last month, we must fetch ALL posts from today back to last month.
                        today = dt.now()
                        days_from_start = (today - start_dt).days + 2 # +2 buffer
                        
                        # Estimate 5 posts/day (avg density)
                        if days_from_start > 0:
                            estimated_posts = days_from_start * 5
                        else:
                            estimated_posts = ((end_dt - start_dt).days + 1) * 5
                            
                        # Cap at higher limit (300) for historical search
                        smart_limit = min(estimated_posts, 300)
                        logger.info(f"📊 Historical Search: {days_from_start} days back → Smart limit: {smart_limit} posts")
                    except Exception as e:
                        logger.warning(f"⚠️ Date calc error: {e}")
                        smart_limit = 150  # Safe default
                else:
                    # Full fetch with original logic (but still cap at reasonable limit)
                    smart_limit = min(max_results, 150)
                    logger.info(f"🔄 Full fetch: limit={smart_limit} (capped for quota)")
                
                # --- Proceed to External Fetch ---
                
                # OPTIMIZATION: Skip Graph API (always fails with 400) and go directly to Apify
                # This saves ~5-10 seconds of failed API attempts
                logger.info(f"⚡ Fetching Facebook data via Apify (optimized)...")
                
                scraper = create_scraper(platform_str)
                use_graph_api = False
                page_info = {}
                
                # ✨ CONCURRENT FETCH: Posts + Page Info at the same time
                # Instead of sequential (posts → page info = 40s+40s = 80s),
                # run them in parallel so total = max(40s, 40s) = 40s.
                # Cache is checked first (instant) before calling Apify.
                
                is_quick_scan = smart_limit <= 5
                import concurrent.futures
                
                def _fetch_posts_concurrent():
                    logger.info(f"📝 [Concurrent] Fetching posts for @{username}... (limit: {smart_limit})")
                    return scraper.get_user_videos(channel_url or username, max_results=smart_limit, until_date=start_date)
                
                def _fetch_page_info_concurrent():
                    """Try DB cache first (instant), then Apify if cache miss."""
                    try:
                        cached = FacebookPageCache.objects.get(username=username)
                        if not cached.is_expired():
                            logger.info(f"💾 [Concurrent] Page info from CACHE: {cached.followers_count:,} followers")
                            return {
                                'username': cached.username,
                                'name': cached.page_name,
                                'display_name': cached.page_name,
                                'avatar_url': cached.avatar_url,
                                'followers': int(cached.followers_count),
                                'likes': int(cached.likes_count),
                                'source': 'cache'
                            }
                    except FacebookPageCache.DoesNotExist:
                        pass
                    
                    # Cache miss → fetch from Apify
                    logger.info(f"📊 [Concurrent] Cache MISS → fetching page info from Apify (page scraper)...")
                    try:
                        fresh_info = scraper.get_page_info(username)
                        logger.info(f"✅ [Concurrent] Page info from Apify: keys={list(fresh_info.keys()) if fresh_info else []}")
                        # Store in cache via get_or_fetch normalizer
                        if fresh_info:
                            FacebookPageCache.get_or_fetch(username=username, fetch_callback=lambda _: fresh_info)
                        return fresh_info or {}
                    except Exception as e:
                        logger.warning(f"⚠️ [Concurrent] Page info Apify failed: {str(e)[:100]}")
                        return {}
                
                # Start both tasks concurrently
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    posts_future = executor.submit(_fetch_posts_concurrent)
                    page_future = executor.submit(_fetch_page_info_concurrent)
                    
                    # Collect posts result (primary - required)
                    try:
                        raw_results = posts_future.result(timeout=120)
                        logger.info(f"✅ Fetched {len(raw_results)} posts")
                    except Exception as fetch_error:
                        page_future.cancel()
                        error_msg = str(fetch_error)
                        logger.error(f"❌ External fetch failed: {error_msg}")
                        
                        # Fallback to DB cache on ANY external fetch error (Timeout, Limit, Blocked, etc.)
                        if db_videos:
                            logger.info(f"⚠️ External fetch failed. Falling back to DB cache ({len(db_videos)} videos)...")
                            normalized = []
                            for vid in db_videos:
                                normalized.append({
                                    'video_id': vid.video_id, 'title': vid.title,
                                    'description': vid.description, 'video_url': vid.video_url,
                                    'thumbnail_url': vid.thumbnail_url, 'likes_count': vid.likes_count,
                                    'comments_count': vid.comments_count, 'shares_count': vid.shares_count,
                                    'views_count': vid.views_count, 'published_at': vid.published_at,
                                    'timestamp': int(vid.published_at.timestamp()) if vid.published_at else 0,
                                    'author_name': vid.author_name, 'author_username': vid.author_username,
                                    'platform': platform_str.lower(), 'raw_data': vid.raw_data
                                })
                            first_vid = db_videos[0]
                            age_hours = round((timezone.now() - first_vid.created_at).total_seconds() / 3600, 1) if first_vid.created_at else 0
                            return Response({
                                'results': normalized,
                                'profile': {'username': username, 'display_name': first_vid.author_name or username,
                                            'avatar_url': first_vid.thumbnail_url, 'followers': 0, 'likes': 0},
                                'total': len(normalized),
                                'source': 'database_cache_fallback',
                                'warning': f'External fetch failed: {error_msg}. Showing cached data.',
                                'cache_age_hours': age_hours
                            })
                        else:
                            logger.warning(f"⚠️ External fetch failed and no cache available. Returning empty response.")
                            return Response({
                                'results': [],
                                'profile': {'username': username, 'display_name': username,
                                            'avatar_url': '', 'followers': 0, 'likes': 0},
                                'total': 0,
                                'source': 'empty_fallback',
                                'warning': f'External fetch failed: {error_msg}.'
                            })
                    
                    # Collect page info result (best-effort, don't block on it too long)
                    page_timeout = 55 if is_quick_scan else 90
                    try:
                        page_info = page_future.result(timeout=page_timeout) or {}
                        dbg_followers = page_info.get('followers') or page_info.get('followersCount') or 0
                        logger.info(f"✅ Page info ready: followers={dbg_followers}, source={page_info.get('source', 'apify_fresh')}")
                    except concurrent.futures.TimeoutError:
                        logger.warning(f"⏰ Page info timed out after {page_timeout}s, proceeding without it")
                        page_info = {}
                    except Exception as pe:
                        logger.warning(f"⚠️ Page info error: {pe}")
                        page_info = {}


                
                
                # Normalize data
                if use_graph_api:
                    # Graph API posts are already normalized
                    normalized = raw_results
                else:
                    # Apify data needs normalization
                    normalized = []
                    for v in raw_results:
                        norm = scraper.normalize_video_data(v)
                        if norm:
                            # Quan trọng: Khi dùng channel_url (profile.php?id=...) để fetch,
                            # author_username trong kết quả có thể là URL hoặc path,
                            # ghi đè về username gốc (numeric ID) để DB lookup khớp.
                            if channel_url and norm.get('author_username', '') != username:
                                norm['author_username'] = username
                            normalized.append(norm)
                
                # Filter by date range if end_date is provided
                if end_date and normalized:

                    try:
                        from datetime import datetime as dt
                        end_dt = dt.strptime(end_date, '%Y-%m-%d')
                        start_dt = dt.strptime(start_date, '%Y-%m-%d') if start_date else None
                        
                        filtered_normalized = []
                        for post in normalized:
                            post_val = post.get('published_at')
                            if post_val:
                                try:
                                    # Normalize post_dt to be offset-naive or aware matching the range
                                    post_dt = None
                                    if isinstance(post_val, dt):
                                        post_dt = post_val
                                    elif isinstance(post_val, str):
                                        # parsing ISO string
                                        # Replace Z with +00:00 to ensure fromisoformat works
                                        clean_str = post_val.replace('Z', '+00:00')
                                        post_dt = dt.fromisoformat(clean_str)
                                    elif isinstance(post_val, (int, float)):
                                        post_dt = dt.fromtimestamp(post_val)
                                    
                                    if post_dt:
                                        # Make naive for comparison to start_dt/end_dt (which are naive from strptime)
                                        # Or better: make start/end aware if post_dt is aware
                                        if post_dt.tzinfo is not None and post_dt.tzinfo.utcoffset(post_dt) is not None:
                                            # Convert to naive (remove timezone) or UTC
                                            # Simplest: remove tzinfo
                                            post_dt = post_dt.replace(tzinfo=None)
                                    
                                        # Check if post is within date range
                                        # Filter is: start_dt <= post_dt <= end_dt
                                        # start_dt is 00:00:00, end_dt should be 23:59:59
                                        
                                        # Adjust end_dt to end of day
                                        real_end_dt = end_dt.replace(hour=23, minute=59, second=59)

                                        if start_dt and post_dt < start_dt:
                                            continue
                                        if post_dt > real_end_dt:
                                            continue
                                    
                                        filtered_normalized.append(post)
                                    else:
                                        filtered_normalized.append(post)
                                except Exception as e:
                                    # If date parsing fails, keep the post (safe fallback)
                                    # logger.warning(f"Date check failed for post: {e}")
                                    filtered_normalized.append(post)
                            else:
                                # If no date, keep the post
                                filtered_normalized.append(post)
                        
                        normalized = filtered_normalized
                        logger.info(f"📅 Date range filter: {len(filtered_normalized)} posts between {start_date} and {end_date}")
                    except Exception as e:
                        logger.warning(f"Date filtering failed: {e}")
            
            
            # --- INSTAGRAM: Use Instagram Apify Service ---
            elif platform_str.upper() == 'INSTAGRAM':
                logger.info(f"📸 Using Instagram Apify Service for @{username}")
                from ..services.instagram_apify_service import InstagramApifyService
                
                instagram_service = InstagramApifyService()
                page_info = {}
                
                try:
                    # Fetch profile info (Non-blocking / Optional)
                    try:
                        profile_info = instagram_service.get_profile_info(username)
                        page_info = profile_info
                        logger.info(f"✅ Instagram Profile: {profile_info.get('followersCount', 0):,} followers")
                    except Exception as e:
                        logger.warning(f"⚠️ Instagram profile fetch failed: {e}. Proceeding to fetch posts...")
                        page_info = {
                            'username': username,
                            'fullName': username,
                            'followersCount': 0,
                            'profilePicUrl': f"https://www.instagram.com/{username}/profile_pic.jpg"
                        }
                    
                    # Fetch posts only if max_results > 0
                    if max_results and max_results > 0:
                        # This returns detailed, normalized data from the service
                        posts_data = instagram_service.get_user_posts_and_reels(username, max_results=max_results)
                        raw_results = posts_data
                    else:
                        # Profile only mode - no posts fetched
                        logger.info(f"📊 Profile-only mode: Skipping posts fetch for @{username}")
                        raw_results = []
                    
                    # Normalize posts (Note: instagram_service already returns mostly normalized data)
                    normalized = []
                    for post in raw_results:
                        # The service returns snake_case keys (likes_count, etc.)
                        # But also preserves 'raw_data' which has the original camelCase keys if needed.
                        # We use the snake_case keys from the service which are reliable.
                        
                        # Infer author name from first post if profile fetch failed
                        if not page_info.get('fullName') and post.get('author_name'):
                             page_info['fullName'] = post.get('author_name')
                             
                        short_code = post.get('short_code') or post.get('video_id', '')
                        post_url = post.get('url') or (f"https://www.instagram.com/p/{short_code}/" if short_code else '')
                        
                        norm = {
                            'video_id': post.get('video_id') or short_code,
                            'short_code': short_code,
                            'url': post_url,
                            'title': (post.get('caption') or '')[:100],
                            'caption': post.get('caption') or '',
                            'description': post.get('caption') or '',
                            'video_url': post.get('video_url'),
                            'thumbnail_url': post.get('thumbnail_url') or '',
                            'likes': post.get('likes_count', 0),
                            'likes_count': post.get('likes_count', 0),
                            'comments': post.get('comments_count', 0),
                            'comments_count': post.get('comments_count', 0),
                            'shares_count': post.get('shares_count', 0),
                            'views': post.get('video_view_count', 0),
                            'views_count': post.get('video_view_count', 0),
                            'published_at': post.get('timestamp'),
                            'timestamp': post.get('timestamp'),
                            'author_name': page_info.get('fullName') or post.get('author_name') or username,
                            'author_username': username,
                            'platform': 'instagram',
                            'is_video': post.get('content_type') == 'reel' or bool(post.get('video_url')),
                            'content_type': 'video' if post.get('content_type') == 'reel' else 'image', 
                            'raw_data': post.get('raw_data', {})
                        }
                        normalized.append(norm)
                    
                    logger.info(f"✅ Instagram: Fetched {len(normalized)} posts for @{username}")
                    
                    # Filter by date range if provided
                    if start_date and end_date and normalized:
                        try:
                            from datetime import datetime
                            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
                            end_dt = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
                            
                            filtered_normalized = []
                            no_date_posts = []  # posts without date → keep as fallback
                            for post in normalized:
                                pub_at = post.get('published_at') or post.get('timestamp')
                                if not pub_at:
                                    no_date_posts.append(post)
                                    continue
                                
                                # Parse the timestamp - support multiple formats
                                try:
                                    if isinstance(pub_at, (int, float)):
                                        # Unix epoch timestamp
                                        pub_dt = datetime.utcfromtimestamp(pub_at)
                                    elif isinstance(pub_at, str):
                                        # Remove timezone info for naive comparison
                                        clean_date = pub_at.split('+')[0].split('Z')[0].replace('T', ' ').strip()
                                        try:
                                            pub_dt = datetime.strptime(clean_date, '%Y-%m-%d %H:%M:%S')
                                        except:
                                            pub_dt = datetime.strptime(clean_date[:10], '%Y-%m-%d')
                                    elif hasattr(pub_at, 'replace'):
                                        pub_dt = pub_at.replace(tzinfo=None)
                                    else:
                                        no_date_posts.append(post)
                                        continue
                                    
                                    if start_dt <= pub_dt <= end_dt:
                                        filtered_normalized.append(post)
                                except Exception as parse_err:
                                    logger.warning(f"Could not parse date {pub_at}: {parse_err}")
                                    no_date_posts.append(post)
                                    continue
                            
                            logger.info(f"📅 Instagram date filter: {len(filtered_normalized)}/{len(normalized)} posts between {start_date} and {end_date}")
                            
                            # Fallback: if filter returns 0 posts, return all fetched posts
                            # (account may not have posted in selected period)
                            if filtered_normalized:
                                normalized = filtered_normalized
                            else:
                                logger.info(f"⚠️ No posts in date range → returning all {len(normalized)} posts (account may not post daily)")
                                # Keep all posts but add warning to response later
                        except Exception as e:
                            logger.warning(f"Instagram date filtering failed: {e}")
                    
                except Exception as e:
                    logger.error(f"❌ Instagram fetch failed: {e}", exc_info=True)
                    return Response({
                        'success': False,
                        'error': f'Failed to fetch Instagram data: {str(e)}'
                    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            # --- OTHER PLATFORMS: Use Apify ---
            else:
                logger.info(f"⚡ Using Apify to fetch videos for @{username} on {platform_str}")
                # TikTok: still use main scraper for videos; stats come from user-scraper
                scraper = create_scraper(platform_str)
                page_info = {}
                raw_results = scraper.get_user_videos(username, max_results=max_results, until_date=start_date)
                
                # Apify data needs normalization
                normalized = []
                for v in raw_results:
                    norm = scraper.normalize_video_data(v)
                    if norm:
                        normalized.append(norm)



            # Extract profile data
            profile_data = None
            # For Facebook: if page_info has data (from cache), create profile even if no posts
            if not raw_results and platform_str.upper() == 'FACEBOOK' and page_info:
                logger.info(f"📊 No posts but have page_info - creating basic profile from cache")
                profile_data = {
                    'username': username,
                    'display_name': page_info.get('name') or page_info.get('display_name') or username,
                    'avatar_url': page_info.get('avatar_url') or page_info.get('image') or page_info.get('profilePic') or '',
                    'follower_count': page_info.get('followers') or 0,
                    'total_likes': page_info.get('likes') or 0,
                    'total_videos': 0,
                    'total_views': 0,
                    'engagement_rate': 0.0,
                    'platform': platform_str,
                    'metadata': {
                        'video_count': 0,
                        'post_count': 0,
                        'fetched_likes_sum': 0,
                        'fetched_comments_sum': 0,
                        'page_info_source': 'cache_only'
                    }
                }
            elif raw_results:
                first_item = raw_results[0]
                
                # --- FACEBOOK SPECIFIC EXTRACTION ---
                if platform_str.upper() == 'FACEBOOK':
                    # Facebook posts scraper returns list of posts, no global authorMeta
                    # We must aggregate from what we have
                    
                    # 1. Basic Info from first post
                    # First check normalized data for cleaner access
                    first_norm = normalized[0] if normalized else {}
                    username = first_norm.get('author_username') or username
                    display_name = first_norm.get('author_name') or username
                    avatar_url = first_norm.get('thumbnail_url', '') # Fallback to first post thumb if no avatar
                    
                    # Try to find better avatar in raw_data (scraper_one uses 'author')
                    user_obj = first_item.get('user', {}) or first_item.get('author', {})
                    if isinstance(user_obj, dict):
                        if user_obj.get('profilePicture'): avatar_url = user_obj.get('profilePicture')  # scraper_one
                        elif user_obj.get('profilePic'): avatar_url = user_obj.get('profilePic')
                        elif user_obj.get('id'): 
                            avatar_url = f"https://graph.facebook.com/{user_obj.get('id')}/picture?type=large"
                    
                    # 2. Aggregated Stats
                    # 2. Aggregated Stats
                    count_posts = len(normalized)
                    count_videos = sum(1 for v in normalized if v.get('video_url'))
                    
                    fetched_views = sum(v.get('views_count', 0) for v in normalized)
                    fetched_likes = sum(v.get('likes_count', 0) for v in normalized)
                    fetched_comments = sum(v.get('comments_count', 0) for v in normalized)
                    fetched_shares = sum(v.get('shares_count', 0) for v in normalized)
                    
                    # Try to extract follower count if available in any post's raw data
                    follower_count = 0
                    for v in normalized:
                        raw = v.get('raw_data', {})
                        # Check user object
                        user = raw.get('user') or {}
                        f_count = user.get('followers') or user.get('followersCount') or user.get('followerCount')
                        if f_count:
                            try:
                                follower_count = int(str(f_count).replace(',', '').replace('.', ''))
                                break
                            except:
                                pass
                        
                        # Check 'page_info' or distinct fields if Apify provides them
                        if raw.get('page_followers'):
                             try:
                                follower_count = int(str(raw.get('page_followers')).replace(',', ''))
                                break
                             except:
                                pass

                    # 3. Engagement
                    engagement_rate = 0.0
                    if fetched_views > 0:
                        engagement_rate = ((fetched_likes + fetched_comments + fetched_shares) / fetched_views) * 100
                    
                    
                    # Use follower count as Total Likes for Page (User Request: "tổng like là tổng like trên trang")
                    # Priority 1: From page_info (facebook-pages-scraper)
                    # Priority 2: From post raw data (rare/unreliable)
                    # Priority 3: 0 (UI will show sum of video likes as fallback, or 0)
                    
                    likes_count = 0  # Initialize outside to avoid UnboundLocalError

                    if page_info:
                        # Common keys in facebook-pages-scraper
                        # 'likes' -> Page Likes
                        # 'followers' -> Page Followers
                        p_likes = page_info.get('likes') or page_info.get('likesCount')
                        p_followers = page_info.get('followers') or page_info.get('followersCount')
                        
                        # Parse if string
                        if isinstance(p_followers, str):
                            try: follower_count = int(p_followers.replace(',', '').replace('.', ''))
                            except: pass
                        elif isinstance(p_followers, (int, float)):
                            follower_count = int(p_followers)

                        # Parse Likes separately
                        if isinstance(p_likes, str):
                            try: likes_count = int(p_likes.replace(',', '').replace('.', ''))
                            except: pass
                        elif isinstance(p_likes, (int, float)):
                            likes_count = int(p_likes)
                                
                        # Update avatar if pages scraper has better one
                        if page_info.get('image'): avatar_url = page_info.get('image')
                        elif page_info.get('profilePic'): avatar_url = page_info.get('profilePic')

                    # FIXED: Do NOT fallback to follower_count. Keep them distinct.
                    page_total_likes = likes_count 
                    
                    profile_data = {
                        'username': username,
                        'display_name': display_name,
                        'avatar_url': avatar_url,
                        'follower_count': follower_count,
                        'total_likes': page_total_likes, # UPDATED: Page Likes only
                        'total_videos': count_posts, 
                        'total_views': fetched_views,
                        'engagement_rate': round(engagement_rate, 2),
                        'platform': platform_str,
                        'metadata': {
                            'video_count': count_videos,
                            'post_count': count_posts,
                            'fetched_likes_sum': fetched_likes, # Keep the sum available in metadata
                            'fetched_comments_sum': fetched_comments,
                            'page_info_source': 'facebook_pages_scraper' if page_info else 'none'
                        }
                    }

                # --- TIKTOK / DOUYIN / INSTAGRAM SPECIFIC EXTRACTION ---
                elif platform_str.upper() == 'INSTAGRAM':
                    # Instagram uses page_info from Instagram Apify Service
                    if page_info:
                        # DEBUG: Log what keys are in page_info
                        logger.info(f"🔍 page_info keys: {list(page_info.keys())}")
                        
                        # DEBUG: Log avatar-related fields
                        avatar_keys = [k for k in page_info.keys() if any(x in k.lower() for x in ['pic', 'avatar', 'image', 'photo'])]
                        logger.info(f"🖼️ Avatar-related keys in page_info: {avatar_keys}")
                        for k in avatar_keys:
                            logger.info(f"   {k} = {str(page_info.get(k))[:100]}")
                        
                        username = page_info.get('username') or username
                        display_name = page_info.get('fullName') or username
                        # Enhanced avatar extraction - match Apify instagram-profile-scraper keys
                        avatar_url = (
                            page_info.get('profilePicUrlHD') or   # Apify: profilePicUrlHD
                            page_info.get('profilePicUrlHd') or 
                            page_info.get('profilePicUrl') or 
                            page_info.get('profile_pic_url') or 
                            page_info.get('profilePictureUrl') or
                            page_info.get('profilePic') or
                            ''
                        )
                        logger.info(f"📸 Extracted avatar for {username}: {avatar_url[:80] if avatar_url else 'EMPTY'}...")
                        follower_count = page_info.get('followersCount') or 0
                        total_posts = page_info.get('postsCount') or len(normalized)
                        
                        # Calculate aggregated stats from fetched posts
                        fetched_likes = sum(v.get('likes_count', 0) for v in normalized)
                        fetched_comments = sum(v.get('comments_count', 0) for v in normalized)
                        fetched_views = sum(v.get('views_count', 0) for v in normalized)
                        
                        # Engagement Rate Calculation
                        engagement_rate = 0.0
                        if follower_count > 0:
                            # Instagram engagement = (likes + comments) / followers * 100
                            engagement_rate = ((fetched_likes + fetched_comments) / follower_count) * 100
                        
                        # ===== DEBUG: Log profile_data =====
                        logger.info("=" * 80)
                        logger.info("📤 SENDING TO FRONTEND - Profile Data:")
                        logger.info("=" * 80)
                        
                        profile_data = {
                            'username': username,
                            'display_name': display_name,
                            'avatar_url': avatar_url,
                            'follower_count': follower_count,
                            'following_count': page_info.get('followingCount', 0),
                            'posts_count': total_posts,  # Total posts (photos + videos + reels)
                            'total_likes': fetched_likes,  # Sum of likes from fetched posts
                            'total_videos': len([v for v in normalized if (v.get('content_type') or '').lower() in ['video', 'reel', 'sidecar']]) or len(normalized),
                            'total_views': fetched_views,
                            'engagement_rate': round(engagement_rate, 2),
                            'platform': platform_str,
                            'is_verified': page_info.get('isVerified', False),
                            'is_private': page_info.get('isPrivate', False),
                            'biography': page_info.get('biography', ''),
                            'external_url': page_info.get('externalUrl', ''),
                            'category': page_info.get('category', ''),
                            'metadata': {
                                'is_verified': page_info.get('isVerified', False),
                                'biography': page_info.get('biography', ''),
                                'external_url': page_info.get('externalUrl', '')
                            }
                        }
                        
                        # Log profile data
                        logger.info("📊 Profile Data to Frontend:")
                        for key, value in profile_data.items():
                            if key != 'metadata':
                                logger.info(f"   • {key:20s} = {value}")
                        logger.info("=" * 80)
                        
                    else:
                        # Fallback if no page_info  
                        profile_data = {
                            'username': username,
                            'display_name': username,
                            'avatar_url': '',
                            'follower_count': 0,
                            'posts_count': len(normalized),  # Fallback: count fetched posts
                            'total_likes': sum(v.get('likes_count', 0) for v in normalized),
                            'total_videos': len(normalized),
                            'total_views': sum(v.get('views_count', 0) for v in normalized),
                            'engagement_rate': 0.0,
                            'platform': platform_str
                        }
                        
                        logger.info("⚠️ Fallback Profile Data (no page_info):")
                        for key, value in profile_data.items():
                            logger.info(f"   • {key:20s} = {value}")
                        logger.info("=" * 80)
                
                # --- YOUTUBE SPECIFIC EXTRACTION ---
                elif platform_str.upper() == 'YOUTUBE':
                    # apify/youtube-scraper returns channel info in each item
                    channel_name = first_item.get('channelName', '') or first_item.get('channel', '') or username
                    channel_id = first_item.get('channelId', '') or username
                    avatar_url = first_item.get('channelAvatarUrl', '') or first_item.get('authorThumbnail', '')

                    # Try to parse subscriber count (can be "1.2M subscribers" string)
                    raw_subs = first_item.get('numberOfSubscribers', 0) or first_item.get('subscriberCount', 0) or 0
                    if isinstance(raw_subs, str):
                        clean_subs = raw_subs.lower().replace(',', '').replace(' subscribers', '').replace(' subscriber', '').strip()
                        try:
                            if 'm' in clean_subs:
                                raw_subs = int(float(clean_subs.replace('m', '')) * 1_000_000)
                            elif 'k' in clean_subs:
                                raw_subs = int(float(clean_subs.replace('k', '')) * 1_000)
                            else:
                                raw_subs = int(float(clean_subs))
                        except Exception:
                            raw_subs = 0
                    follower_count = int(raw_subs) if raw_subs else 0

                    total_channel_videos = first_item.get('channelTotalVideos', 0) or len(normalized)
                    total_channel_views = first_item.get('channelTotalViews', 0) or 0

                    fetched_views = sum(v.get('views_count', 0) for v in normalized)
                    fetched_likes = sum(v.get('likes_count', 0) for v in normalized)
                    fetched_comments = sum(v.get('comments_count', 0) for v in normalized)

                    engagement_rate = 0.0
                    if follower_count > 0 and fetched_views > 0:
                        engagement_rate = ((fetched_likes + fetched_comments) / fetched_views) * 100

                    logger.info(f"📺 YouTube channel: {channel_name}, subs={follower_count:,}, videos={total_channel_videos}")

                    profile_data = {
                        'username': channel_id or username,
                        'display_name': channel_name,
                        'avatar_url': avatar_url,
                        'follower_count': follower_count,
                        'subscribers_count': follower_count,
                        'total_likes': fetched_likes,
                        'total_videos': total_channel_videos,
                        'total_views': total_channel_views or fetched_views,
                        'engagement_rate': round(engagement_rate, 2),
                        'platform': platform_str,
                        'metadata': {
                            'video_count': len(normalized),
                            'fetched_likes_sum': fetched_likes,
                            'fetched_comments_sum': fetched_comments,
                            'channel_url': first_item.get('channelUrl', ''),
                        }
                    }

                # --- TIKTOK / DOUYIN SPECIFIC EXTRACTION ---
                else: 
                    # With apidojo/tiktok-scraper, profile info lives under "channel".
                    # For backwards-compatibility we also support legacy authorMeta/authorStats.
                    raw = first_item
                    logger.info(f"🔍 TikTok Raw Item Keys: {list(raw.keys())}")
                    channel = raw.get('channel') or {}
                    if raw.get('authorMeta'):
                        logger.info(f"👤 authorMeta: {raw.get('authorMeta')}")
                    if raw.get('authorStats'):
                        logger.info(f"📊 authorStats: {raw.get('authorStats')}")
                    
                    author_meta = (
                        channel
                        or raw.get('authorMeta')
                        or raw.get('author')
                        or raw.get('raw_data', {}).get('authorMeta')
                        or {}
                    )
                    
                    # Robust extraction with multiple fallbacks
                    username = (
                        author_meta.get('username')
                        or author_meta.get('name')
                        or author_meta.get('uniqueId')
                        or author_meta.get('unique_id')
                        or username
                    )
                    display_name = (
                        author_meta.get('display_name')
                        or author_meta.get('nickname')
                        or author_meta.get('nickName')
                        or username
                    )
                    
                    # Avatar strategies
                    avatar_url = (
                        author_meta.get('avatar')
                        or author_meta.get('avatarLarger')
                        or author_meta.get('avatarMedium')
                        or author_meta.get('avatarThumb')
                        or author_meta.get('avatarUrl')
                        or ''
                    )
                    
                    # Stats strategies - prefer channel fields, then legacy authorStats
                    author_stats = raw.get('authorStats') or author_meta
                    
                    follower_count = int(
                        author_meta.get('followers')
                        or author_meta.get('followersCount')
                        or author_stats.get('fans')
                        or author_stats.get('followerCount')
                        or author_stats.get('followers')
                        or 0
                    )
                    total_likes = int(
                        author_meta.get('likes')
                        or author_meta.get('heart')
                        or author_stats.get('heart')
                        or author_stats.get('heartCount')
                        or author_stats.get('diggCount')
                        or 0
                    )
                    total_videos = int(
                        author_meta.get('videos')
                        or author_meta.get('video')
                        or author_stats.get('videoCount')
                        or author_stats.get('videoCount')
                        or len(normalized)
                    )
                    
                    # Calculate aggregated stats from fetched videos
                    fetched_views = sum(v.get('views_count', 0) for v in normalized)
                    fetched_likes = sum(v.get('likes_count', 0) for v in normalized)
                    fetched_comments = sum(v.get('comments_count', 0) for v in normalized)
                    fetched_shares = sum(v.get('shares_count', 0) for v in normalized)

                    # Skip apidojo/tiktok-user-scraper when clockworks already has profile stats
                    # (fans, heart, video, avatar) - saves ~30s and extra Apify credits
                    has_profile_from_video = bool(follower_count or total_likes or avatar_url)
                    if not has_profile_from_video:
                        try:
                            user_stats = fetch_tiktok_user_profile(username)
                            if user_stats:
                                follower_count = int(user_stats.get('followers') or follower_count)
                                total_likes = int(user_stats.get('likes') or total_likes)
                                total_videos = int(user_stats.get('videos') or total_videos)
                                avatar_url = user_stats.get('avatar') or avatar_url
                        except Exception as e:
                            logger.error(f"⚠️ TikTok user scraper failed for {username}: {e}")

                    # Fallbacks when stats still missing
                    if total_likes == 0 and fetched_likes > 0:
                        total_likes = fetched_likes
                    if not avatar_url and normalized:
                        # Use first video thumbnail as a lightweight avatar fallback
                        avatar_url = normalized[0].get('thumbnail_url') or avatar_url

                    # Engagement Rate Calculation
                    engagement_rate = 0.0
                    if fetched_views > 0:
                        engagement_rate = ((fetched_likes + fetched_comments + fetched_shares) / fetched_views) * 100
                    elif follower_count > 0:
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
                    
                    # Save to TikTokUserCache
                    try:
                        from ..models import TikTokUserCache
                        
                        TikTokUserCache.objects.update_or_create(
                            username=username,
                            defaults={
                                'display_name': display_name,
                                'avatar_url': avatar_url,
                                'followers_count': follower_count,
                                'likes_count': total_likes,
                                'videos_count': total_videos,
                                'raw_data': profile_data,
                                'expires_at': timezone.now() + timedelta(hours=24)
                            }
                        )
                        logger.info(f"💾 Saved TikTok profile to cache: {username}")
                    except Exception as e:
                        logger.error(f"⚠️ Failed to save TikTok cache: {e}")
                
                logger.info(f"📊 Profile data extracted: {profile_data['follower_count']:,} followers, {profile_data['engagement_rate']}% engagement")
            
            # --- INSTAGRAM PROFILE-ONLY: raw_results rỗng nhưng page_info có (max_results=0) ---
            if profile_data is None and platform_str.upper() == 'INSTAGRAM' and page_info:
                logger.info("📸 Instagram profile-only mode: building profile_data from page_info (no posts)")
                username = page_info.get('username') or username
                display_name = page_info.get('fullName') or username
                avatar_url = (
                    page_info.get('profilePicUrlHD') or
                    page_info.get('profilePicUrlHd') or
                    page_info.get('profilePicUrl') or
                    page_info.get('profile_pic_url') or
                    ''
                )
                profile_data = {
                    'username': username,
                    'display_name': display_name,
                    'avatar_url': avatar_url,
                    'follower_count': page_info.get('followersCount') or 0,
                    'following_count': page_info.get('followingCount', 0),
                    'posts_count': page_info.get('postsCount', 0),
                    'total_likes': 0,
                    'total_videos': 0,
                    'total_views': 0,
                    'engagement_rate': 0.0,
                    'platform': platform_str,
                    'is_verified': page_info.get('isVerified', False),
                    'is_private': page_info.get('isPrivate', False),
                    'biography': page_info.get('biography', ''),
                    'external_url': page_info.get('externalUrl', ''),
                    'category': page_info.get('category', ''),
                }
                logger.info(f"✅ Profile-only profile_data: avatar_url={avatar_url[:80] if avatar_url else 'EMPTY'}...")

            # Save videos
            ordered_videos = []
            
            if normalized:
                # Instagram: Skip DB save for now, just return normalized data
                if platform_str.upper() == 'INSTAGRAM':
                    logger.info(f"📦 Returning {len(normalized)} Instagram posts (DB save skipped)")
                    ordered_videos = normalized
                else:
                    # Other platforms: Save to DB
                    # BLOCK DETECTION: If Apify returns very few results compared to existing DB cache,
                    # it likely got blocked. In this case, save the new posts (merge)
                    # but return the FULL DB cache to avoid showing degraded data.
                    is_likely_blocked = False
                    if db_videos and len(normalized) < max(3, len(db_videos) * 0.3):
                        is_likely_blocked = True
                        logger.warning(
                            f"⚠️ BLOCK DETECTED: Apify returned only {len(normalized)} posts "
                            f"but DB has {len(db_videos)}. Saving new posts but returning full DB cache."
                        )
                    
                    saved_ok = False
                    try:
                        # Always save what we got (merge new posts into DB)
                        saved_videos = scraper.save_videos(normalized)
                        saved_ok = True
                        
                        if is_likely_blocked:
                            # Return full DB cache (re-query to get merged data)
                            full_db_videos = list(ScrapedVideo.objects.filter(
                                platform=Platform[platform_str.upper()],
                                author_username=username
                            ).order_by('-published_at')[:max_results])
                            ordered_videos = full_db_videos
                            logger.info(f"🔄 Block recovery: returning {len(ordered_videos)} videos from DB cache")
                        else:
                            # Normal path: map back to preserve order
                            video_id_to_video = {v.video_id: v for v in saved_videos}
                            for norm in normalized:
                                vid = norm.get('video_id')
                                if vid in video_id_to_video:
                                    ordered_videos.append(video_id_to_video[vid])
                    
                    except Exception as e:
                        logger.error(f"Error saving videos: {e}")
                        saved_ok = False
                    
                    # Fallback: if save failed or returned 0 videos, use normalized dicts directly
                    if not saved_ok:
                        logger.warning(f"⚠️ DB save failed or returned 0. Using normalized dicts as fallback.")
                        ordered_videos = normalized  # dicts, not model objects

            # Serialize videos
            # For Instagram and Facebook fallback (dict data), serialize directly
            if platform_str.upper() == 'INSTAGRAM':
                results_data = ordered_videos  # Already normalized dicts
            elif isinstance(ordered_videos[0] if ordered_videos else None, dict):
                # ordered_videos contains dicts (fallback mode)
                # Convert datetime fields to ISO string for JSON serialization
                results_data = []
                for item in ordered_videos:
                    serialized = dict(item)
                    pub_at = serialized.get('published_at')
                    if pub_at and hasattr(pub_at, 'isoformat'):
                        serialized['published_at'] = pub_at.isoformat()
                    # Ensure numeric fields are ints
                    for field in ('likes_count', 'views_count', 'comments_count', 'shares_count'):
                        if serialized.get(field) is None:
                            serialized[field] = 0
                    results_data.append(serialized)
            else:
                # Model objects - use VideoSerializer
                videos_serializer = VideoSerializer(ordered_videos, many=True)
                results_data = videos_serializer.data
            
            response_data = {
                'success': True,
                'platform': platform_str,
                'username': username,
                'count': len(ordered_videos),
                'results': results_data,
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

