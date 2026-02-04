"""
Apify scraper service for all platforms.

This service uses Apify actors to scrape data from TikTok, Instagram,
Facebook, and Douyin.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from django.conf import settings
from apify_client import ApifyClient
from apify_client.clients import ActorClient

from .base_scraper import (
    BaseScraperService,
    ScraperException,
    ScraperTimeoutException,
    ScraperNotFoundException
)
from ..models import Platform

logger = logging.getLogger(__name__)


class ApifyScraperService(BaseScraperService):
    """
    Apify-based scraper service supporting multiple platforms.
    
    This service uses Apify actors to scrape data from various social media
    platforms in a unified way.
    """
    
    def __init__(self, platform: Platform):
        """
        Initialize Apify scraper.
        
        Args:
            platform: Target platform to scrape
            
        Raises:
            ScraperException: If Apify is not configured
        """
        super().__init__(platform)
        
        # Get Apify configuration
        self.api_token = getattr(settings, 'APIFY_API_TOKEN', '')
        if not self.api_token:
            raise ScraperException("APIFY_API_TOKEN not configured")
        
        # Initialize Apify client
        self.client = ApifyClient(self.api_token)
        
        # Get actor ID for platform
        actors = getattr(settings, 'APIFY_ACTORS', {})
        self.actor_id = actors.get(platform.value, '')
        
        if not self.actor_id:
            raise ScraperException(f"No Apify actor configured for {platform}")
        
        self.timeout = getattr(settings, 'APIFY_TIMEOUT', 300)
        self.max_results_limit = getattr(settings, 'APIFY_MAX_RESULTS', 100)
        
        self.logger.info(
            f"Initialized Apify scraper for {platform} "
            f"using actor: {self.actor_id}"
        )
    
    def _build_tiktok_input(
        self,
        keyword: str,
        max_results: int = 20
    ) -> Dict[str, Any]:
        """Build input for TikTok Apify actor."""
        return {
            "searchQueries": [keyword],
            "resultsPerPage": min(max_results, self.max_results_limit),
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False,
            "shouldDownloadSlideshowImages": False,
        }
    
    def _build_instagram_input(
        self,
        keyword: str,
        max_results: int = 20
    ) -> Dict[str, Any]:
        """Build input for Instagram Apify actor."""
        # Clean hashtag - remove # and spaces
        clean_keyword = keyword.replace('#', '').replace(' ', '').strip()
        
        # Use directUrls format which works on free tier
        # Build Instagram hashtag explore URL
        instagram_url = f"https://www.instagram.com/explore/tags/{clean_keyword}/"
        
        return {
            "directUrls": [instagram_url],
            "resultsLimit": min(max_results, self.max_results_limit),
        }
    
    def _build_instagram_reels_input(
        self,
        keyword: str,
        max_results: int = 20
    ) -> Dict[str, Any]:
        """Build input for Instagram Reel Scraper actor."""
        # For hashtag search, we need to use Instagram Hashtag Scraper with resultsType=reels
        # Or search by username if keyword starts with @
        # Remove spaces for proper hashtag/username formatting
        clean_keyword = keyword.replace('#', '').replace('@', '').replace(' ', '').strip()
        
        if keyword.startswith('@'):
            # Search user's reels
            return {
                "username": [clean_keyword],
                "resultsLimit": min(max_results, self.max_results_limit),
            }
        else:
            # Search hashtag reels - use hashtag scraper with reels filter
            return {
                "hashtags": [clean_keyword],
                "resultsLimit": min(max_results, self.max_results_limit),
                "resultsType": "reels",  # Only get reels
                "searchType": "hashtag",
            }
    
    def _build_facebook_input(
        self,
        keyword: str,
        max_results: int = 20,
        username: Optional[str] = None,
        until_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build input for Facebook Apify actor."""
        input_data = {}
        if username:
            # Crawl a specific page/user
            # Clean username just in case
            clean_user = username.replace('@', '').strip()
            
            # SMART LIMIT: Based strictly on frontend calculation (8 * N days)
            effective_limit = max_results 
            
            if until_date:
                from datetime import datetime
                try:
                    start_date = datetime.strptime(until_date, '%Y-%m-%d')
                    days_back = (datetime.now() - start_date).days
                    
                    if days_back > 0:
                         # Just log it, we trust the effective_limit from frontend calculation
                         self.logger.info(f"📊 Date range: {days_back} days → Target {effective_limit} posts (Strict Limit)")
                except:
                    pass
            else:
                 effective_limit = max_results
            
            # If we have a date filter, trust the Frontend calculation.
            # Strategy: Use strict limit provided by FS (No buffer needed per user request)
            if until_date:
                # effective_limit came from frontend (e.g. 24 for 3 days)
                limit_to_use = min(effective_limit, 300) 
            else:
                limit_to_use = effective_limit

            input_data = {
                "startUrls": [{"url": f"https://www.facebook.com/{clean_user}"}],
                "resultsLimit": limit_to_use,
                "maxPostCount": limit_to_use,
                # Optimize: Focus ONLY on Timeline/Posts
                "proxyConfiguration": {"useApifyProxy": True},
                "maxRequestRetries": 1,
                "maxComments": 0,
                "scrapeAbout": False,
                "scrapeReviews": False,
                "scrapePhotos": False,
                "scrapeVideos": False, 
            }
        else:
            # Fallback to search query (if supported by actor, or might need different actor)
            input_data = {
                "searchQuery": keyword,
                "maxPosts": min(max_results, self.max_results_limit),
            }
            
        # Add date filtering - only scrape posts recent enough
        if until_date:
            # until_date is the Start Date of the selected range (e.g. 2026-01-01)
            # We tell scraper to stop when it sees a post OLDER than this date.
            input_data['maxPostDate'] = until_date
            self.logger.info(f"⏱️ Smart Stop enabled: Scraper will halt at {until_date}")
            
        return input_data
    
    def _build_actor_input(
        self,
        keyword: str,
        max_results: int = 20,
        username: Optional[str] = None,
        until_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Build actor input based on platform.
        
        Args:
            keyword: Search keyword
            max_results: Maximum results
            username: Username for user-specific searches
            
        Returns:
            Actor input dictionary
        """
        if self.platform == Platform.TIKTOK:
            if username:
                # Optimized Fetch Logic (Similar to Facebook)
                effective_limit = max_results
                
                # Use strict limit from FE (days * 8) without extra buffer
                # User confirmed realistic daily max is ~10 videos
                limit_to_use = min(effective_limit, 300)

                # OPTIMIZATION: If fetching small batch (e.g. Add Channel), disable video checking/downloading
                # This makes the initial scan much faster (seconds instead of minutes)
                should_download_videos = True
                if max_results <= 10:
                    should_download_videos = False
                
                input_data = {
                    "profiles": [username],
                    "resultsPerPage": limit_to_use,
                    "postsCount": limit_to_use,
                    "maxItems": limit_to_use, # Explicitly limit items
                    "shouldDownloadVideos": should_download_videos, 
                    "shouldDownloadCovers": True, 
                }
                
                # Add date filtering (similar to maxPostDate in Facebook)
                if until_date:
                    input_data["oldestPostDate"] = until_date
                    self.logger.info(f"⏱️ TikTok Smart Stop enabled: Scraper will halt at {until_date}")
                    
                return input_data
            return self._build_tiktok_input(keyword, max_results)
        
        elif self.platform == Platform.INSTAGRAM:
            if username:
                # For Instagram profiles, use both resultsLimit and postsLimit
                return {
                    "usernames": [username],
                    "resultsLimit": min(max_results, self.max_results_limit),
                    "postsLimit": min(max_results, self.max_results_limit),  # Alternative field
                }
            return self._build_instagram_input(keyword, max_results)
        
        elif self.platform == Platform.FACEBOOK:
            if username:
                return self._build_facebook_input(keyword, max_results, username, until_date)
            else:
                # Use Global Search Actor
                actors = getattr(settings, 'APIFY_ACTORS', {})
                # Use a specific search actor (e.g., moJalo4813/facebook-search-scraper or similar)
                # Fallback to the default 'facebook' key if 'facebook_search' not found, 
                # though strictly speaking standard post scraper doesn't do global search well.
                self.actor_id = actors.get('facebook_search', 'moJalo4813/facebook-search-scraper')
                self.logger.info(f"Switching to Facebook Search Scraper ({self.actor_id}) for keyword: {keyword}")
                
                return {
                    "searchQuery": keyword,
                    "maxPosts": min(max_results, self.max_results_limit),
                }
        
        elif self.platform == Platform.DOUYIN:
            # Similar to TikTok
            return self._build_tiktok_input(keyword, max_results)
        
        else:
            raise ScraperException(f"Unsupported platform: {self.platform}")
    
    def run_actor(
        self,
        actor_input: Dict[str, Any],
        timeout: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Run Apify actor and get results.
        
        Args:
            actor_input: Input parameters for the actor
            timeout: Timeout in seconds (uses default if None)
            
        Returns:
            List of results from actor
            
        Raises:
            ScraperException: If actor run fails
            ScraperTimeoutException: If actor times out
        """
        timeout = timeout or self.timeout
        
        try:
            self.logger.info(f"Running Apify actor: {self.actor_id}")
            self.logger.debug(f"Actor input: {actor_input}")
            
            # Run the actor
            run = self.client.actor(self.actor_id).call(
                run_input=actor_input,
                timeout_secs=timeout
            )
            
            # Check run status
            if run['status'] != 'SUCCEEDED':
                error_msg = f"Actor run failed with status: {run['status']}"
                self.logger.error(error_msg)
                raise ScraperException(error_msg)
            
            # Get results from dataset
            dataset_id = run.get('defaultDatasetId')
            if not dataset_id:
                raise ScraperException("No dataset ID in actor run")
            
            self.logger.info(f"Fetching results from dataset: {dataset_id}")
            
            # Fetch all items from dataset
            items = []
            for item in self.client.dataset(dataset_id).iterate_items():
                items.append(item)
            
            self.logger.info(f"Retrieved {len(items)} items from Apify")
            return items
            
        except Exception as e:
            if 'timeout' in str(e).lower():
                raise ScraperTimeoutException(f"Actor run timed out: {str(e)}")
            raise ScraperException(f"Actor run failed: {str(e)}")
    
    def search_videos(
        self,
        keyword: str,
        min_likes: int = 0,
        min_views: int = 0,
        max_results: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Search for videos using Apify.
        
        Args:
            keyword: Search keyword or hashtag
            min_likes: Minimum likes (filtering done after scraping)
            min_views: Minimum views (filtering done after scraping)
            max_results: Maximum results to fetch
            
        Returns:
            List of raw video data from Apify
        """
        try:
            actor_input = {}
            # Special handling for Instagram Hashtag search
            if self.platform == Platform.INSTAGRAM and not keyword.startswith('@') and not keyword.startswith('http'):
                # Switch to hashtag scraper for better results count
                actors = getattr(settings, 'APIFY_ACTORS', {})
                self.actor_id = actors.get('instagram_hashtag', 'apify/instagram-hashtag-scraper')
                self.logger.info(f"Switching to Hashtag Scraper ({self.actor_id}) for keyword: {keyword}")
                
                # Build specific input for hashtag scraper
                clean_keyword = keyword.replace('#', '').replace(' ', '').strip()
                actor_input = {
                    "hashtags": [clean_keyword],
                    "resultsLimit": min(max_results, self.max_results_limit),
                    "searchType": "hashtag"
                }
            else:
                # Default input builder
                actor_input = self._build_actor_input(keyword, max_results)
                
            results = self.run_actor(actor_input)
            
            self.logger.info(
                f"Found {len(results)} videos for keyword: {keyword}"
            )
            
            return results
            
        except Exception as e:
            self.logger.error(f"Search failed: {str(e)}", exc_info=True)
            raise ScraperException(f"Search failed: {str(e)}")
    
    def get_page_info(self, username: str) -> Dict[str, Any]:
        """
        Get page/profile info using a specialized actor (e.g. facebook-pages-scraper).
        Currently only implemented for Facebook.
        """
        if self.platform != Platform.FACEBOOK:
            return {}

        try:
             # fast checking using lightweight scrape if possible
             actors = getattr(settings, 'APIFY_ACTORS', {})
             page_actor_id = actors.get('facebook_page', 'apify/facebook-pages-scraper')
             
             self.logger.info(f"Fetching Page Info for {username} using {page_actor_id}")
             
             # Clean user
             clean_user = username.replace('@', '').strip()
             if 'facebook.com' in clean_user:
                 # Extract username/id if full url given
                 parts = clean_user.rstrip('/').split('/')
                 clean_user = parts[-1]

             run_input = {
                "startUrls": [{"url": f"https://www.facebook.com/{clean_user}"}],
                "maxItems": 1
             }
             
             # Use the client to call specifically this actor, ignoring self.actor_id which is for posts
             run = self.client.actor(page_actor_id).call(
                run_input=run_input,
                timeout_secs=60 # Short timeout for metadata
             )
             
             if run['status'] == 'SUCCEEDED':
                 dataset_id = run.get('defaultDatasetId')
                 if dataset_id:
                     items = list(self.client.dataset(dataset_id).iterate_items())
                     if items:
                         return items[0]
             
             return {}

        except Exception as e:
            self.logger.warning(f"Failed to fetch page info: {e}")
            return {}

    def get_user_videos(
        self,
        username: str,
        max_results: int = 20,
        until_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get videos from a specific user.
        
        Args:
            username: Username or user ID
            max_results: Maximum results
            
        Returns:
            List of raw video data
        """
        try:
            actor_input = self._build_actor_input(
                keyword="",  # Not used for user searches
                max_results=max_results,
                username=username,
                until_date=until_date
            )
            results = self.run_actor(actor_input)
            
            self.logger.info(
                f"Found {len(results)} videos for user: {username}"
            )
            
            return results
            
        except Exception as e:
            self.logger.error(f"User video fetch failed: {str(e)}", exc_info=True)
            raise ScraperException(f"User video fetch failed: {str(e)}")
    
    def normalize_video_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize Apify video data to common format.
        
        Args:
            raw_data: Raw data from Apify actor
            
        Returns:
            Normalized video data
        """
        # Platform-specific normalization
        if self.platform == Platform.TIKTOK or self.platform == Platform.DOUYIN:
            return self._normalize_tiktok_data(raw_data)
        elif self.platform == Platform.INSTAGRAM:
            return self._normalize_instagram_data(raw_data)
        elif self.platform == Platform.FACEBOOK:
            return self._normalize_facebook_data(raw_data)
        else:
            raise ScraperException(f"Unsupported platform: {self.platform}")
    
    def _normalize_tiktok_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize TikTok/Douyin data from Apify free-tiktok-scraper."""
        # Video ID
        video_id = data.get('id', '')
        
        # Author info - support multiple formats
        author_meta = data.get('authorMeta', {}) or data.get('author', {})
        author_username = author_meta.get('name', '') or author_meta.get('uniqueId', '') or author_meta.get('unique_id', '')
        author_name = author_meta.get('nickname', '') or author_username
        
        # Stats - try root first, then 'stats', then 'statistics' object
        stats = data.get('stats', {}) or data.get('statistics', {})
        likes_count = data.get('diggCount') or stats.get('diggCount') or stats.get('digg_count', 0)
        views_count = data.get('playCount') or stats.get('playCount') or stats.get('play_count', 0)
        comments_count = data.get('commentCount') or stats.get('commentCount') or stats.get('comment_count', 0)
        shares_count = data.get('shareCount') or stats.get('shareCount') or stats.get('share_count', 0)
        
        # URLs
        video_url = data.get('webVideoUrl', '')
        if not video_url and author_username and video_id:
             video_url = f"https://www.tiktok.com/@{author_username}/video/{video_id}"
        
        # Download URL
        download_url = ''
        video_meta = data.get('videoMeta', {}) or data.get('video', {})
        if video_meta and isinstance(video_meta, dict):
             download_url = video_meta.get('downloadAddr', '') or video_meta.get('playAddr', '') or video_meta.get('play_addr', '')
        
        if not download_url:
            media_urls = data.get('mediaUrls', [])
            if media_urls and isinstance(media_urls, list) and len(media_urls) > 0:
                download_url = media_urls[0]
        
        # Thumbnail - from videoMeta cover or authorMeta avatar as fallback
        thumbnail_url = ''
        if video_meta and isinstance(video_meta, dict):
            thumbnail_url = (
                video_meta.get('coverUrl', '') or
                video_meta.get('cover', '') or
                video_meta.get('dynamicCover', '') or
                video_meta.get('originCover', '')
            )
        
        
        # REMOVED Fallback to author avatar. We want video-specific covers only.
        # if not thumbnail_url and author_meta:
        #     thumbnail_url = author_meta.get('avatar', '')
        
        # Music info - from musicMeta
        music_meta = data.get('musicMeta', {})
        music_title = music_meta.get('musicName', '') if music_meta else ''
        music_author = music_meta.get('musicAuthor', '') if music_meta else ''
        
        # Duration extraction
        duration = data.get('videoMeta', {}).get('duration', 0)
        
        # Published time
        create_time = data.get('createTime') or data.get('createTimeISO')
        
        return {
            'video_id': str(video_id),
            'duration': duration, # Add duration to normalized data
            'title': data.get('text', ''),
            'description': data.get('text', ''),
            'author_username': author_username,
            'author_name': author_name,
            'likes_count': int(likes_count) if likes_count else 0,
            'views_count': int(views_count) if views_count else 0,
            'comments_count': int(comments_count) if comments_count else 0,
            'shares_count': int(shares_count) if shares_count else 0,
            'video_url': video_url,
            'download_url': download_url,
            'thumbnail_url': thumbnail_url,
            'published_at': self._parse_timestamp(create_time),
            'hashtags': [tag.get('name', '') for tag in data.get('hashtags', [])],
            'music_info': {
                'title': music_title,
                'author': music_author,
                'url': '',
            },
            'raw_data': data
        }
    
    def _normalize_instagram_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Instagram data."""
        # Thumbnail fallback logic
        thumbnail = ''
        if data.get('images') and len(data['images']) > 0:
            thumbnail = data['images'][0]
        else:
            thumbnail = data.get('displayUrl', '') or data.get('thumbnailUrl', '')

        # Author fallback logic
        author_username = data.get('ownerUsername', '')
        if not author_username and data.get('owner'):
             author_username = data['owner'].get('username', '')
             
        # Normalize stats (ensure int)
        likes = data.get('likesCount', 0)
        views = data.get('videoViewCount', 0) or data.get('videoPlayCount', 0)

        return {
            'video_id': data.get('id') or data.get('shortCode', ''),
            'title': data.get('caption', ''),
            'description': data.get('caption', ''),
            'author_username': author_username,
            'author_name': data.get('ownerFullName', ''),
            'likes_count': int(likes) if likes else 0,
            'views_count': int(views) if views else 0,
            'comments_count': data.get('commentsCount', 0),
            'shares_count': 0,  # Instagram doesn't provide this
            'video_url': data.get('url') or data.get('postUrl', ''),
            'download_url': data.get('videoUrl', ''),
            'thumbnail_url': thumbnail,
            'published_at': self._parse_timestamp(data.get('timestamp') or data.get('date')),
            'hashtags': data.get('hashtags', []),
            'music_info': {},
            'raw_data': data
        }
    
    def _normalize_facebook_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Facebook data."""
        # Clean ID
        post_id = data.get('postId') or data.get('id', '')
        
        # Stats
        likes = data.get('likes', 0)
        comments = data.get('comments', 0)
        shares = data.get('shares', 0)
        
        # Parse 'shares' if it's a dict (sometimes Apify returns { count: 123 }) or int
        if isinstance(shares, dict):
            shares = shares.get('count', 0)
        
        # User Info
        user_data = data.get('user', {})
        author_username = ''
        author_name = ''
        
        if isinstance(user_data, dict):
            author_username = user_data.get('username') or user_data.get('id', '')
            author_name = user_data.get('name', '')
        
        # Fallback for flat structure or missing user object
        if not author_username:
             author_username = data.get('postAuthor', '') or data.get('pageName', '')
        if not author_name:
             author_name = data.get('postAuthor', '') or data.get('pageName', '')
             
        # Extract from URL if still empty
        if not author_username:
            post_url = data.get('url') or data.get('postUrl', '')
            if 'facebook.com/' in post_url:
                try:
                    parts = post_url.split('facebook.com/')[1].split('/')
                    if parts:
                        author_username = parts[0]
                except:
                    pass

        # Media (Video/Image)
        video_url = data.get('videoUrl', '')
        
        # THUMBNAIL EXTRACTION STRATEGY
        # 1. Direct keys
        thumbnail_url = (
            data.get('image') or 
            data.get('imageUrl') or 
            data.get('thumbnail') or 
            data.get('fullImage') or
            ''
        )

        # 2. Images list (often contains high-res photo URL)
        if not thumbnail_url and data.get('images') and isinstance(data.get('images'), list):
            if len(data['images']) > 0:
                thumbnail_url = data['images'][0]

        # 3. Attachments (common for link previews or album covers)
        if not thumbnail_url and data.get('attachments') and isinstance(data.get('attachments'), list):
            for att in data['attachments']:
                if att.get('media', {}).get('image', {}).get('src'):
                    thumbnail_url = att['media']['image']['src']
                    break
                    
        # 4. Preferred Thumbnail (Common for videos at root)
        if not thumbnail_url and data.get('preferred_thumbnail'):
             pref = data.get('preferred_thumbnail')
             if isinstance(pref, dict):
                 if pref.get('image') and pref['image'].get('uri'):
                     thumbnail_url = pref['image']['uri']
        
        # Construct Web URL (Permalink) - MUST BE BEFORE is_video detection
        permalink = data.get('url') or data.get('postUrl') or ''
        
        # Determine if it's a video
        # Apify fb scraper often puts isVideo=True at root
        is_video = data.get('isVideo', False)

        # Detect from URL if it's a Reel
        if not is_video and permalink and ('/reel/' in permalink or '/videos/' in permalink):
            is_video = True
            
        # Detect if videoUrl exists at root
        if not is_video and (data.get('videoUrl') or data.get('video_url')):
            is_video = True
            video_url = data.get('videoUrl') or data.get('video_url') or video_url

        # 5. Advanced "media" list Extraction
        if data.get('media'):
            for m in data.get('media', []):
                # Check for Video
                # Some actors use 'type': 'video', others might use GraphQL types
                is_media_video = (m.get('type') == 'video' or m.get('__typename') == 'Video')
                
                if is_media_video:
                    is_video = True
                    video_url = m.get('url', '') or m.get('playable_url', '') or video_url
                    
                    # Priority to video thumbnail if available
                    vid_thumb = m.get('thumbnail', '')
                    if not vid_thumb and m.get('thumbnailImage'):
                         vid_thumb = m.get('thumbnailImage', {}).get('uri', '')
                    
                    if vid_thumb:
                        thumbnail_url = vid_thumb
                    break # Focus on the video content
                
                # Check for Photo
                is_photo = (m.get('__typename') == 'Photo' or m.get('__isMedia') == 'Photo' or m.get('type') == 'photo')
                if is_photo and not thumbnail_url:
                    # Photo usually has thumbnail or image.uri
                    thumbnail_url = m.get('thumbnail') or m.get('image', {}).get('uri') or m.get('src') or m.get('url', '')


        # Fallback: if videoUrl exists, it's a video
        if video_url:
            is_video = True
            
        # IMPORTANT: Inject into raw_data so it persists in DB
        data['is_video_derived'] = is_video

        # If no URL but we have postId (and maybe username), construct it
        if not permalink and post_id:
            # Prefer username, fallback to ID, fallback to 'watch' format if video
            user_handle = author_username or data.get('pageName') or 'watch'
            if is_video:
                 permalink = f"https://www.facebook.com/{user_handle}/videos/{post_id}/"
            else:
                 permalink = f"https://www.facebook.com/{user_handle}/posts/{post_id}/"

        return {
            'video_id': str(post_id),
            'title': data.get('text', '') or data.get('message', '') or 'No Content',
            'description': data.get('text', '') or data.get('message', ''),
            'author_username': str(author_username),
            'author_name': str(author_name),
            'likes_count': int(likes) if isinstance(likes, (int, float, str)) and str(likes).isdigit() else 0,
            'views_count': int(data.get('views') or data.get('viewCount') or data.get('videoViewCount') or 0),
            'comments_count': int(comments) if isinstance(comments, (int, float, str)) and str(comments).isdigit() else 0,
            'shares_count': int(shares) if isinstance(shares, (int, float, str)) and str(shares).isdigit() else 0,
            'video_url': permalink, # Normalized Web URL
            'download_url': video_url, # Direct file URL
            'thumbnail_url': thumbnail_url,
            'published_at': self._parse_timestamp(data.get('time') or data.get('timestamp')),
            'hashtags': [],
            'music_info': {},
            'is_video': is_video, # Keep explicit flag for immediate usage
            'raw_data': data
        }
    
    def _parse_timestamp(self, timestamp: Any) -> Optional[datetime]:
        """
        Parse various timestamp formats to datetime.
        
        Args:
            timestamp: Timestamp in various formats
            
        Returns:
            Datetime object or None
        """
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
            self.logger.warning(f"Failed to parse timestamp {timestamp}: {str(e)}")
            return None


# Factory function to create appropriate scraper
def create_scraper(platform: str, search_type: str = 'posts') -> ApifyScraperService:
    """
    Create a scraper instance for the specified platform.
    
    Args:
        platform: Platform name (tiktok, instagram, facebook, douyin)
        search_type: specific search type (e.g. 'reels', 'posts')
        
    Returns:
        ApifyScraperService instance
        
    Raises:
        ValueError: If platform is not supported
    """
    try:
        platform_enum = Platform[platform.upper()]
        
        # Check for specific scraper variants
        if platform_enum == Platform.INSTAGRAM and search_type == 'reels':
            from .instagram_reels_scraper import create_instagram_reels_scraper
            return create_instagram_reels_scraper()
            
        return ApifyScraperService(platform_enum)
        
    except KeyError:
        raise ValueError(
            f"Unsupported platform: {platform}. "
            f"Supported: {', '.join([p.value for p in Platform])}"
        )
