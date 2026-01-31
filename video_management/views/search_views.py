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
        start_date = data.get('start_date') or data.get('until_date')  # Backward compatibility
        end_date = data.get('end_date')
        
        logger.info(f"Processing request for {username} on {platform_str} (start={start_date}, end={end_date})")

        try:
            # --- FACEBOOK: Try Graph API first, fallback to Apify (HYBRID) ---
            if platform_str.upper() == 'FACEBOOK':
                page_info = {}
                raw_results = []
                use_graph_api = False
                
                # Check for force_refresh flag
                force_refresh = request.data.get('force_refresh', False)
                
                # Try to fetch from DB first if not forced to refresh
                db_videos = []
                if not force_refresh:
                    logger.info(f"💾 Checking DB for @{username} on {platform_str}...")
                    db_query = ScrapedVideo.objects.filter(
                        platform=Platform[platform_str.upper()],
                        author_username=username
                    )
                    
                    if start_date:
                        try:
                            # Parse YYYY-MM-DD
                            start_dt_db = datetime.strptime(start_date, '%Y-%m-%d')
                            # Make aware if project uses timezone support
                            if timezone.is_aware(timezone.now()):
                                start_dt_db = timezone.make_aware(start_dt_db)
                            
                            logger.info(f"📅 DB Filter Start: {start_dt_db} (Total before: {db_query.count()})")
                            db_query = db_query.filter(published_at__gte=start_dt_db)
                        except Exception as e:
                            logger.error(f"❌ Date Filter Error (Start): {e}")
                        
                    if end_date:
                        try:
                            # Parse YYYY-MM-DD
                            end_dt_db = datetime.strptime(end_date, '%Y-%m-%d')
                            # End of day (23:59:59)
                            end_dt_db = end_dt_db.replace(hour=23, minute=59, second=59)
                            # Make aware
                            if timezone.is_aware(timezone.now()):
                                end_dt_db = timezone.make_aware(end_dt_db)
                                
                            logger.info(f"📅 DB Filter End: {end_dt_db}")
                            db_query = db_query.filter(published_at__lte=end_dt_db)
                        except Exception as e:
                            logger.error(f"❌ Date Filter Error (End): {e}")
                        
                    # Execute query
                    raw_db_videos = list(db_query.order_by('-published_at'))
                    
                    # Deduplicate by video_id (and normalized ID for Facebook)
                    seen_ids = set()
                    db_videos = []
                    
                    for vid in raw_db_videos:
                        # Normalize ID: Facebook sometimes uses PAGEID_POSTID or just POSTID
                        # We standardized on just POSTID (last part)
                        clean_id = vid.video_id
                        if '_' in clean_id:
                             clean_id = clean_id.split('_')[-1]
                        
                        if clean_id not in seen_ids:
                            seen_ids.add(clean_id)
                            # Also add original just in case mixed usage
                            seen_ids.add(vid.video_id) 
                            
                            db_videos.append(vid)
                            
                    logger.info(f"✅ Found {len(db_videos)} videos in DB (after filtering & deduping).")
                    
                    if db_videos:
                        logger.info(f"✅ Found {len(db_videos)} videos in DB. Skipping external fetch.")
                        # Normalize DB videos to match expected output format
                        normalized = []
                        for vid in db_videos:
                            # Reconstruct dict from model
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
                        
                        # Process profile info from the latest video's raw data if available
                        first_vid = db_videos[0]
                        avatar_url = first_vid.thumbnail_url # Default fallback
                        
                        if first_vid.raw_data:
                            try:
                                # Try to extract profile picture from nested 'user' or 'author' object commonly returned by Apify
                                raw = first_vid.raw_data
                                user_data = raw.get('user') or raw.get('author') or raw.get('owner')
                                
                                if user_data and isinstance(user_data, dict):
                                    avatar_url = (
                                        user_data.get('profilePic') or 
                                        user_data.get('profile_pic_url') or 
                                        user_data.get('avatar') or 
                                        user_data.get('profileImage') or
                                        avatar_url
                                    )
                                else:
                                    # Sometimes it's at root level depending on scraper
                                    avatar_url = (
                                        raw.get('authorProfilePic') or
                                        raw.get('userProfilePic') or
                                        avatar_url
                                    )
                            except: pass
                            
                        # Sort by timestamp desc
                        normalized.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
                        
                        # Calculate aggregates
                        count_posts = len(normalized)
                        fetched_likes = sum(v.get('likes_count', 0) for v in normalized)
                        
                        # Return immediately
                        return Response({
                            'success': True,
                            'platform': platform_str,
                            'username': username,
                            'count': count_posts,
                            'results': normalized,
                            'profile': {
                                'username': username,
                                'display_name': first_vid.author_name or username,
                                'avatar_url': avatar_url, # Enhanced avatar extraction
                                'followers_count': 0, 
                                'total_likes': fetched_likes,
                                'total_videos': count_posts
                            }
                        }, status=status.HTTP_200_OK)
                
                # --- Proceed to External Fetch if no DB data or Force Refresh ---
                
                # Try Graph API first (fast, 2-3s)
                try:
                    logger.info(f"⚡ Trying Facebook Graph API for @{username} (FAST, 2-3s)")
                    from ..services.facebook_graph_service import FacebookGraphService
                    
                    graph_service = FacebookGraphService()
                    
                    # Fetch page metadata
                    logger.info("📊 Fetching page metadata...")
                    graph_data = graph_service.get_page_metadata(username)
                    
                    page_info = {
                        'likes': graph_data.get('fan_count', 0),
                        'followers': graph_data.get('followers_count', 0),
                        'image': graph_data.get('picture_url', ''),
                        'profilePic': graph_data.get('picture_url', ''),
                        'name': graph_data.get('name', ''),
                        'about': graph_data.get('about', ''),
                        'website': graph_data.get('website', ''),
                        'category': graph_data.get('category', ''),
                        'posts_count': graph_data.get('posts_count', 0)
                    }
                    
                    # Fetch posts
                    logger.info("📝 Fetching posts...")
                    raw_results = graph_service.get_page_posts(
                        page_id=username,
                        max_results=max_results or 100,
                        since_date=start_date
                    )
                    
                    logger.info(f"✅ Graph API SUCCESS: {page_info.get('followers', 0):,} followers, {len(raw_results)} posts")
                    use_graph_api = True
                    
                except Exception as graph_error:
                    logger.warning(f"⚠️ Graph API failed (permissions issue): {str(graph_error)[:100]}")
                    logger.info("🔄 Falling back to Apify (optimized, 20-40s)...")
                    
                    # Fallback to Apify (optimized)
                    scraper = create_scraper(platform_str)
                    
                    # Try to fetch page info for accurate Followers/Likes
                    try:
                        logger.info(f"📊 Fetching page info via Apify fallback...")
                        page_info = scraper.get_page_info(username)
                        logger.info(f"✅ Apify Page Info: {page_info.get('followers')} followers")
                    except Exception as e:
                        logger.warning(f"Apify page info fetch failed: {e}")
                        page_info = {}

                    raw_results = scraper.get_user_videos(username, max_results=max_results, until_date=start_date)
                    use_graph_api = False
                
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
                            normalized.append(norm)
                
                # Filter by date range if end_date is provided
                if end_date and normalized:

                    try:
                        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
                        start_dt = datetime.strptime(start_date, '%Y-%m-%d') if start_date else None
                        
                        filtered_normalized = []
                        for post in normalized:
                            post_val = post.get('published_at')
                            if post_val:
                                try:
                                    # Normalize post_dt to be offset-naive or aware matching the range
                                    post_dt = None
                                    if isinstance(post_val, datetime):
                                        post_dt = post_val
                                    elif isinstance(post_val, str):
                                        # parsing ISO string
                                        # Replace Z with +00:00 to ensure fromisoformat works
                                        clean_str = post_val.replace('Z', '+00:00')
                                        post_dt = datetime.fromisoformat(clean_str)
                                    elif isinstance(post_val, (int, float)):
                                        post_dt = datetime.fromtimestamp(post_val)
                                    
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
            
            # --- OTHER PLATFORMS: Use Apify ---
            else:
                logger.info(f"⚡ Using Apify to fetch videos for @{username} on {platform_str}")
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
            if raw_results:
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
                    
                    # Try to find better avatar in raw_data
                    user_obj = first_item.get('user', {})
                    if isinstance(user_obj, dict):
                        # Some versions might have profilePic
                        if user_obj.get('profilePic'): avatar_url = user_obj.get('profilePic')
                        elif user_obj.get('id'): 
                            # Try graph fallback (might be broken but better than nothing)
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
                else: 
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

