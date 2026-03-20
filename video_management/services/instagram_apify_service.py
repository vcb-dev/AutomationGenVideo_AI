"""
Instagram Apify Service - Comprehensive Instagram Data Fetching

- Profile: apify/instagram-profile-scraper (chuyên profile, trả profilePicUrl/profilePicUrlHD)
- Posts/Reels: apify/instagram-scraper (resultsType=posts)

Features:
- Fetch profile info (followers, bio, avatar) via instagram-profile-scraper
- Fetch posts & reels via instagram-scraper
- Automatic data normalization
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from django.conf import settings
from apify_client import ApifyClient

logger = logging.getLogger(__name__)


class InstagramApifyService:
    """
    Comprehensive Instagram scraper using Apify actors.
    
    Combines profile data and content data for complete Instagram analytics.
    """
    
    def __init__(self):
        """Initialize Instagram Apify service."""
        # Get Apify API token
        self.api_token = getattr(settings, 'APIFY_API_TOKEN', '')
        if not self.api_token:
            raise ValueError("APIFY_API_TOKEN not configured in settings")
        
        # Initialize Apify client
        self.client = ApifyClient(self.api_token)
        
        # Get actor IDs from settings
        actors = getattr(settings, 'APIFY_ACTORS', {})
        self.scraper_actor = actors.get('instagram', 'apify/instagram-scraper')
        self.profile_actor = actors.get('instagram_profile', 'apify/instagram-profile-scraper')
        
        self.timeout = getattr(settings, 'APIFY_TIMEOUT', 300)
        self.max_results = getattr(settings, 'APIFY_MAX_RESULTS', 100)
        
        logger.info(
            f"Initialized Instagram Apify Service\n"
            f"  Profile Actor: {self.profile_actor}\n"
            f"  Posts Actor: {self.scraper_actor}"
        )
    
    def get_profile_info(self, username: str) -> Dict[str, Any]:
        """
        Fetch Instagram profile information.
        
        Args:
            username: Instagram username (without @)
            
        Returns:
            Profile data including:
            - username, fullName, biography
            - followersCount, followingCount, postsCount
            - isVerified, isPrivate
            - profilePicUrl, externalUrl
            - joinDate, category
            
        Raises:
            Exception: If profile fetch fails
        """
        try:
            # Clean username
            clean_username = username.replace('@', '').strip()
            
            logger.info(f"📊 Fetching profile info for: {clean_username} (actor: {self.profile_actor})")
            
            # Dùng instagram-profile-scraper - chuyên profile, trả profilePicUrl/profilePicUrlHD đầy đủ
            actor_input = {"usernames": [clean_username]}
            
            run = self.client.actor(self.profile_actor).call(
                run_input=actor_input,
                timeout_secs=60,
                memory_mbytes=1024
            )
            
            # Check run status
            if run['status'] != 'SUCCEEDED':
                raise Exception(f"Profile scraper failed with status: {run['status']}")
            
            # Get results
            dataset_id = run.get('defaultDatasetId')
            if not dataset_id:
                raise Exception("No dataset ID returned from profile scraper")
            
            # Fetch items
            items = list(self.client.dataset(dataset_id).iterate_items())
            
            if not items:
                raise Exception(f"No profile data found for username: {clean_username}")
            
            profile_data = items[0]
            
            # DEBUG: Log ALL keys in profile_data
            logger.info(f"🔍 RAW APIFY PROFILE DATA KEYS: {list(profile_data.keys())}")
            
            # DEBUG: Log avatar-related fields
            avatar_fields = {k: str(v)[:100] for k, v in profile_data.items() if any(x in k.lower() for x in ['pic', 'avatar', 'image', 'photo'])}
            logger.info(f"🖼️ AVATAR FIELDS IN RAW DATA: {avatar_fields}")
            
            logger.info(
                f"✅ Profile fetched: {profile_data.get('username')} | "
                f"Followers: {profile_data.get('followersCount', 0):,} | "
                f"Posts: {profile_data.get('postsCount', 0)}"
            )
            
            return self._normalize_profile_data(profile_data)
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch profile for {username}: {str(e)}")
            raise
    
    def get_user_posts_and_reels(
        self, 
        username: str, 
        max_results: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch all posts and reels from a username using profile scraper.
        
        Args:
            username: Instagram username (without @)
            max_results: Maximum number of posts/reels to fetch (default: from settings)
            
        Returns:
            List of normalized post/reel data
            
        Raises:
            Exception: If content fetch fails
        """
        try:
            # Clean username
            clean_username = username.replace('@', '').strip()
            
            # Use provided max_results or default from settings
            limit = max_results if max_results is not None else self.max_results
            
            logger.info(
                f"📹 Fetching posts & reels for: {clean_username} "
                f"(limit: {limit})"
            )
            
            # Build actor input for instagram-scraper to get posts
            actor_input = {
                "directUrls": [f"https://www.instagram.com/{clean_username}/"],
                "resultsType": "posts",
                "resultsLimit": limit
            }
            
            # Run the instagram scraper actor
            run = self.client.actor(self.scraper_actor).call(
                run_input=actor_input,
                timeout_secs=self.timeout,
                memory_mbytes=1024
            )
            
            # Check run status
            if run['status'] != 'SUCCEEDED':
                raise Exception(f"Profile scraper failed with status: {run['status']}")
            
            # Get results
            dataset_id = run.get('defaultDatasetId')
            if not dataset_id:
                raise Exception("No dataset ID returned from profile scraper")
            
            # Fetch all items
            items = list(self.client.dataset(dataset_id).iterate_items())
            
            if not items:
                logger.warning(f"No data returned for {clean_username}")
                return []
            
            # All items are posts (instagram-scraper returns posts directly)
            posts = items
            
            logger.info(
                f"✅ Fetched {len(posts)} posts/reels from {clean_username}"
            )
            
            # Normalize all items
            normalized_items = []
            for post in posts[:limit]:  # Limit to requested amount
                normalized = self._normalize_post_data(post)
                if normalized:
                    normalized_items.append(normalized)
            
            # Log statistics
            posts_count = sum(1 for item in normalized_items if item.get('content_type') == 'post')
            reels_count = sum(1 for item in normalized_items if item.get('content_type') == 'reel')
            
            logger.info(
                f"📊 Content breakdown: {posts_count} posts, {reels_count} reels"
            )
            
            return normalized_items
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch content for {username}: {str(e)}")
            raise
    
    def get_complete_user_data(
        self, 
        username: str, 
        max_posts: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Fetch complete Instagram user data (profile + posts + reels).
        
        This is the main method to use for comprehensive Instagram analytics.
        
        Args:
            username: Instagram username (without @)
            max_posts: Maximum posts/reels to fetch
            
        Returns:
            Complete data dictionary:
            {
                'profile': {...},  # Profile info
                'content': [...],  # Posts and reels
                'stats': {...}     # Aggregated statistics
            }
        """
        try:
            clean_username = username.replace('@', '').strip()
            
            logger.info(f"🎯 Fetching COMPLETE data for: {clean_username}")
            
            # Fetch profile info
            profile = self.get_profile_info(clean_username)
            
            # Fetch posts and reels
            content = self.get_user_posts_and_reels(clean_username, max_posts)
            
            # Calculate aggregated stats
            stats = self._calculate_stats(content)
            
            result = {
                'profile': profile,
                'content': content,
                'stats': stats,
                'fetched_at': datetime.now().isoformat()
            }
            
            logger.info(
                f"✅ Complete data fetched for {clean_username}:\n"
                f"  Followers: {profile.get('followersCount', 0):,}\n"
                f"  Content items: {len(content)}\n"
                f"  Avg engagement rate: {stats.get('avg_engagement_rate', 0):.2f}%"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch complete data for {username}: {str(e)}")
            raise
    
    def _normalize_profile_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize profile data to consistent format."""
        # ===== DEBUG: LOG ALL APIFY RAW DATA =====
        logger.info("=" * 80)
        logger.info("📦 APIFY INSTAGRAM PROFILE - FULL RAW DATA:")
        logger.info("=" * 80)
        logger.info(f"🔑 ALL KEYS: {list(data.keys())}")
        logger.info("")
        logger.info("📊 PROFILE FIELDS:")
        for key, value in data.items():
            if key not in ['raw_data']:  # Skip raw_data to avoid recursion
                value_str = str(value)[:200] if value else 'NULL'
                logger.info(f"   • {key:30s} = {value_str}")
        logger.info("=" * 80)
        # ========================================
        
        username = data.get('username', '')
        
        # 1. Ưu tiên lấy avatar từ raw data Apify (profilePicUrlHD > profilePicUrl)
        # 2. Fallback: profile_pic.jpg (Instagram endpoint - có thể lỗi 404 trên một số tài khoản)
        profile_pic = (
            data.get('profilePicUrlHD') or
            data.get('profilePicUrlHd') or  # biến thể camelCase
            data.get('profilePicUrl') or
            data.get('profile_pic_url') or
            ''
        )
        if profile_pic and isinstance(profile_pic, str) and profile_pic.startswith('http'):
            logger.info(f"✅ Using avatar from Apify: {profile_pic[:80]}...")
        elif username:
            profile_pic = f"https://www.instagram.com/{username}/profile_pic.jpg"
            logger.info(f"⚠️ Apify không trả avatar, dùng fallback: {profile_pic}")
        else:
            profile_pic = ''
            logger.warning("⚠️ No username and no avatar from Apify")
        
        logger.info(f"📸 Final profilePicUrl: {profile_pic[:80] if profile_pic else 'EMPTY'}...")
        
        return {
            'username': username,
            'fullName': data.get('fullName', ''),
            'biography': data.get('biography', ''),
            'followersCount': data.get('followersCount', 0),
            'followingCount': data.get('followsCount', 0),  # Note: 'followsCount' not 'followingCount'
            'postsCount': data.get('postsCount', 0),
            'isVerified': data.get('verified', False),
            'isPrivate': data.get('private', False),
            'profilePicUrl': profile_pic,
            'externalUrl': data.get('externalUrl', ''),
            'category': data.get('category') or data.get('businessCategoryName', ''),
            'joinDate': data.get('joinDate') or (data.get('about') or {}).get('date_joined', ''),
            'raw_data': data
        }
    
    def _normalize_post_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize post/reel data to consistent format.
        
        Detects if content is a post or reel based on 'productType' field.
        """
        # Determine content type
        product_type = data.get('productType', 'feed')
        is_reel = (product_type == 'clips')
        content_type = 'reel' if is_reel else 'post'
        
        # Get media type
        media_type = data.get('type', 'Image')  # Image, Video, Sidecar
        
        # Extract thumbnail
        thumbnail_url = ''
        if data.get('images') and len(data['images']) > 0:
            thumbnail_url = data['images'][0]
        else:
            thumbnail_url = data.get('displayUrl', '') or data.get('thumbnailUrl', '')
        
        # Extract video URL (for reels and video posts)
        video_url = data.get('videoUrl', '')
        downloaded_video = data.get('downloadedVideo', '')  # Apify-hosted video
        
        # Parse timestamp
        timestamp = self._parse_timestamp(data.get('timestamp'))
        
        return {
            # IDs and URLs
            'video_id': data.get('id', '') or data.get('shortCode', ''),
            'short_code': data.get('shortCode', ''),
            'url': data.get('url', ''),
            
            # Content type classification
            'content_type': content_type,  # 'post' or 'reel'
            'media_type': media_type,      # 'Image', 'Video', 'Sidecar'
            'product_type': product_type,  # 'feed' or 'clips'
            
            # Content
            'caption': data.get('caption', ''),
            'hashtags': data.get('hashtags', []),
            'mentions': data.get('mentions', []),
            
            # Author info
            'author_username': data.get('ownerUsername', ''),
            'author_name': data.get('ownerFullName', ''),
            'author_id': data.get('ownerId', ''),
            
            # Engagement metrics
            'likes_count': int(data.get('likesCount', 0)),
            'comments_count': int(data.get('commentsCount', 0)),
            'shares_count': int(data.get('sharesCount', 0)),
            'video_view_count': int(data.get('videoViewCount', 0) or data.get('videoPlayCount', 0)),
            
            # Media
            'thumbnail_url': thumbnail_url,
            'video_url': video_url,
            'downloaded_video': downloaded_video,
            'images': data.get('images', []),
            
            # Dimensions
            'dimensions_height': data.get('dimensionsHeight', 0),
            'dimensions_width': data.get('dimensionsWidth', 0),
            'video_duration': data.get('videoDuration', 0),
            
            # Metadata
            'timestamp': timestamp,
            'published_at': timestamp,
            'location': data.get('locationName', ''),
            'is_comments_disabled': data.get('isCommentsDisabled', False),
            
            # Music info (for reels)
            'music_info': data.get('musicInfo', {}),
            
            # Raw data
            'raw_data': data
        }
    
    def _calculate_stats(self, content: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate aggregated statistics from content."""
        if not content:
            return {
                'total_posts': 0,
                'total_reels': 0,
                'total_likes': 0,
                'total_comments': 0,
                'total_views': 0,
                'avg_likes': 0,
                'avg_comments': 0,
                'avg_views': 0,
                'avg_engagement_rate': 0
            }
        
        posts = [c for c in content if c['content_type'] == 'post']
        reels = [c for c in content if c['content_type'] == 'reel']
        
        total_likes = sum(c['likes_count'] for c in content)
        total_comments = sum(c['comments_count'] for c in content)
        total_views = sum(c['video_view_count'] for c in content)
        
        avg_likes = total_likes / len(content) if content else 0
        avg_comments = total_comments / len(content) if content else 0
        avg_views = total_views / len(content) if content else 0
        
        # Engagement rate = (likes + comments) / views (for videos) or likes (for images)
        total_engagement = total_likes + total_comments
        engagement_base = total_views if total_views > 0 else total_likes
        avg_engagement_rate = (total_engagement / engagement_base * 100) if engagement_base > 0 else 0
        
        return {
            'total_posts': len(posts),
            'total_reels': len(reels),
            'total_content': len(content),
            'total_likes': total_likes,
            'total_comments': total_comments,
            'total_views': total_views,
            'avg_likes': round(avg_likes, 2),
            'avg_comments': round(avg_comments, 2),
            'avg_views': round(avg_views, 2),
            'avg_engagement_rate': round(avg_engagement_rate, 2)
        }
    
    def _parse_timestamp(self, timestamp: Any) -> Optional[datetime]:
        """Parse various timestamp formats to datetime."""
        if not timestamp:
            return None
        
        try:
            # If already datetime
            if isinstance(timestamp, datetime):
                return timestamp
            
            # If Unix timestamp (integer or float)
            if isinstance(timestamp, (int, float)):
                return datetime.fromtimestamp(timestamp)
            
            # If ISO string
            if isinstance(timestamp, str):
                from dateutil import parser
                return parser.parse(timestamp)
            
            return None
            
        except Exception as e:
            logger.warning(f"Failed to parse timestamp {timestamp}: {str(e)}")
            return None


# Convenience function
def fetch_instagram_data(username: str, max_posts: Optional[int] = None) -> Dict[str, Any]:
    """
    Convenience function to fetch complete Instagram data.
    
    Args:
        username: Instagram username
        max_posts: Maximum posts/reels to fetch
        
    Returns:
        Complete Instagram data (profile + content + stats)
    """
    service = InstagramApifyService()
    return service.get_complete_user_data(username, max_posts)
