"""
Douyin Channel Profile Views

Fetch full profile of a Douyin channel (followers, likes, avatar...).
Called ONLY on "Update" click — NOT during search (to save Apify credits).

Apify actor: natanielsantos/douyin-scraper
Strategy: fetch only 1 post but enable scrapeAdditionalUserInfo=True
to get full author profile metadata at minimal cost (~2 events).
"""

import logging
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
from apify_client import ApifyClient

logger = logging.getLogger(__name__)


@api_view(['POST'])
def fetch_douyin_channel_profile(request):
    """
    Fetch full Douyin channel profile (followers, likes, avatar, etc).

    POST /api/douyin/profile/
    Body: { "username": "someuser" }

    Returns full channel profile including:
    - follower_count
    - total_likes (heart count)
    - avatar_url
    - display_name
    - video_count
    - engagement_rate (calculated)
    
    TROUBLESHOOTING:
    If follower_count, total_likes are returning 0, it means:
    1. The channel might be PRIVATE (Apify can't access)
    2. The username might not exist
    3. Apify API might be rate limited
    4. Apify actor response format changed - check console logs for field names
    """
    username = request.data.get('username', '').strip().lstrip('@')

    if not username:
        return Response(
            {'success': False, 'error': 'username is required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    api_token = getattr(settings, 'APIFY_API_TOKEN', '')
    if not api_token:
        return Response(
            {'success': False, 'error': 'APIFY_API_TOKEN not configured'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    actor_id = getattr(settings, 'APIFY_ACTOR_DOUYIN', 'natanielsantos/douyin-scraper')

    try:
        client = ApifyClient(api_token)

        # ====== STRATEGY: Try multiple approaches to get profile data ======
        profile = None
        
        # APPROACH 1: Search by @username with scrapeAdditionalUserInfo enabled
        logger.info(f"[DouyinProfile] APPROACH 1: Searching for @{username} with full user info...")
        profile = _fetch_profile_by_search(client, actor_id, username)
        
        if profile and (profile.get('follower_count', 0) > 0 or profile.get('total_likes', 0) > 0):
            logger.info(f"[DouyinProfile] ✅ APPROACH 1 SUCCESS: @{username}")
            return Response({
                'success': True,
                'profile': profile,
            }, status=status.HTTP_200_OK)
        
        # APPROACH 2: If followers/likes are 0, try fetching MORE posts to get better data
        if profile:
            logger.info(f"[DouyinProfile] APPROACH 1 returned 0 followers/likes. Trying APPROACH 2: Fetch more posts...")
            profile_extended = _fetch_profile_by_search(client, actor_id, username, max_posts=10)
            if profile_extended and (profile_extended.get('follower_count', 0) > 0):
                logger.info(f"[DouyinProfile] ✅ APPROACH 2 SUCCESS: @{username}")
                return Response({
                    'success': True,
                    'profile': profile_extended,
                }, status=status.HTTP_200_OK)
            # If extended also returns 0, use original profile (at least has avatar and other data)
            profile = profile_extended or profile
        
        # If we got here, return whatever data we have
        if profile:
            logger.warning(
                f"[DouyinProfile] ⚠️ Limited data for @{username}: "
                f"followers={profile.get('follower_count')}, likes={profile.get('total_likes')} "
                f"- Channel might be private or Apify couldn't fetch full data"
            )
            return Response({
                'success': True,
                'profile': profile,
            }, status=status.HTTP_200_OK)
        
        # Failed to get any profile
        logger.error(f"[DouyinProfile] ❌ Failed to fetch profile for @{username}")
        # instead of returning 404, return a normal 200 with success=false so
        # the frontend can show the "private/non‑existent" message without
        # triggering an HTTP error. also supply a minimal empty profile
        return Response(
            {
                'success': False,
                'error': 'No data found for this username',
                'profile': {
                    'username': username,
                    'display_name': username,
                    'avatar_url': '',
                    'follower_count': 0,
                    'total_likes': 0,
                    'total_videos': 0,
                    'total_views': 0,
                    'engagement_rate': 0,
                },
            },
            status=status.HTTP_200_OK
        )

    except Exception as e:
        logger.error(f"[DouyinProfile] Failed for @{username}: {str(e)}", exc_info=True)
        return Response(
            {'success': False, 'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


def _fetch_profile_by_search(client, actor_id: str, username: str, max_posts: int = 3) -> dict:
    """
    Fetch Douyin profile by searching for username.
    
    Args:
        client: Apify client instance
        actor_id: Apify actor ID
        username: Douyin username (without @)
        max_posts: Maximum posts to fetch (more posts = better chance of getting profile data)
    
    Returns:
        Dictionary with profile data or None if failed
    """
    try:
        # Search by username — fetch with full user info
        search_term = f"@{username}" if not username.startswith('@') else username
        
        actor_input = {
            "searchTermsOrHashtags": [search_term],
            "sortBy": "latest",
            "publishTime": "all",
            "maxItemsPerUrl": max_posts,
            "maxPosts": max_posts,
            # Enable profile info add-on to get followers, heart count, video count
            "scrapeAdditionalUserInfo": True,   # ← Gets followers, heart count, video count
            "scrapePlayCount": True,            # ← Gets real view counts
            "scrapeUserPostCount": True,        # ← Gets total video count on channel
            "scrapeHashtags": True,
            "scrapeAuthors": True,
            # Keep expensive add-ons off to save costs
            "shouldDownloadCovers": False,
            "shouldDownloadVideos": False,
            "shouldDownloadMusic": False,
        }

        logger.debug(f"[DouyinProfile] Actor input: {actor_input}")

        run = client.actor(actor_id).call(
            run_input=actor_input,
            timeout_secs=getattr(settings, 'APIFY_TIMEOUT', 300)
        )

        if run['status'] != 'SUCCEEDED':
            logger.error(f"[DouyinProfile] Apify actor failed: {run['status']}")
            return None

        # Collect items
        items = list(client.dataset(run.get('defaultDatasetId')).iterate_items())

        if not items:
            logger.warning(f"[DouyinProfile] No items found for @{username}")
            return None

        logger.info(f"[DouyinProfile] Retrieved {len(items)} items for @{username}")

        # Extract author profile from first item
        first = items[0]
        author = first.get('authorMeta') or first.get('author') or {}

        # Debug: Log what fields are available
        logger.debug(f"[DouyinProfile] Author fields available: {list(author.keys())}")
        logger.debug(f"[DouyinProfile] Author data: {author}")

        # --- Followers ---
        # Apify returns: followersCount (camelCase, can be null)
        follower_count = (
            author.get('followersCount')  # Correct field name from Apify
            or author.get('fans')
            or author.get('followerCount')
            or author.get('follower_count')
            or 0
        )
        
        # Log actual value before fallback
        raw_followers = author.get('followersCount')
        if raw_followers is None:
            logger.warning(f"[DouyinProfile] followersCount is NULL for @{username}")
        elif raw_followers == 0:
            logger.warning(f"[DouyinProfile] followersCount is 0 for @{username}")

        # --- Total Likes (heart) ---
        # Apify returns: heartCount (camelCase, can be null)
        total_likes = (
            author.get('heartCount')  # Correct field name from Apify
            or author.get('heart')
            or author.get('diggCount')
            or author.get('total_likes')
            or 0
        )
        
        # Log actual value before fallback
        raw_likes = author.get('heartCount')
        if raw_likes is None:
            logger.warning(f"[DouyinProfile] heartCount is NULL for @{username}")
        elif raw_likes == 0:
            logger.warning(f"[DouyinProfile] heartCount is 0 for @{username}")

        # --- Total Videos on channel ---
        # Apify returns: videoCount (camelCase, can be null)
        total_videos = (
            author.get('videoCount')  # Correct field name from Apify
            or author.get('video')
            or author.get('total_video')
            or len(items)
        )

        # --- Display Name ---
        display_name = (
            author.get('nickName')
            or author.get('nickname')
            or author.get('name')
            or first.get('author_name')
            or username
        )

        # --- Resolved username (may differ from input) ---
        resolved_username = (
            author.get('uniqueId')
            or author.get('unique_id')
            or author.get('username')
            or username
        )

        # --- Avatar URL ---
        avatar_url = ''
        if author.get('avatarThumb'):
            avatar_url = author['avatarThumb']
        elif author.get('avatar_thumb'):
            thumb = author['avatar_thumb']
            if isinstance(thumb, dict) and thumb.get('url_list'):
                avatar_url = thumb['url_list'][0]
        elif author.get('avatar'):
            avatar_url = author['avatar']

        # --- Engagement Rate ---
        # = (total_likes / followers) * 100  — channel-level metric
        engagement_rate = round((total_likes / follower_count * 100), 2) if follower_count > 0 else 0.0

        # --- Views from fetched videos ---
        stats = first.get('statistics', {}) or {}
        total_views = sum(
            (v.get('statistics', {}) or {}).get('playCount', 0) or 0
            for v in items
        )

        profile = {
            'username': resolved_username,
            'display_name': display_name,
            'avatar_url': avatar_url,
            'follower_count': int(follower_count),
            'total_likes': int(total_likes),
            'total_videos': int(total_videos),
            'total_views': int(total_views),
            'engagement_rate': engagement_rate,
        }

        logger.info(
            f"[DouyinProfile] @{username}: "
            f"followers={follower_count}, likes={total_likes}, "
            f"videos={total_videos}, engagement={engagement_rate}%, avatar={bool(avatar_url)}"
        )

        return profile

    except Exception as e:
        logger.error(f"[DouyinProfile] _fetch_profile_by_search failed: {str(e)}", exc_info=True)
        return None
