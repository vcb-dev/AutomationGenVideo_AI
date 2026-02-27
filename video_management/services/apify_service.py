"""
Apify scraper service for all platforms.

This service uses Apify actors to scrape data from TikTok, Instagram,
Facebook, and Douyin.
"""

import logging
import random
import unicodedata
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
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


def fetch_tiktok_user_profile(username: str) -> Optional[Dict[str, Any]]:
    """
    Fetch TikTok user profile stats (followers, likes, videos, avatar)
    using the dedicated apidojo/tiktok-user-scraper actor.

    This returns a single user object for the requested username,
    ignoring follower lists to keep token usage low.
    """
    api_token = getattr(settings, "APIFY_API_TOKEN", "")
    if not api_token:
        return None

    actors = getattr(settings, "APIFY_ACTORS", {})
    actor_id = actors.get("tiktok_user")
    if not actor_id:
        return None

    client = ApifyClient(api_token)
    clean_user = username.replace("@", "").strip()
    profile_url = f"https://www.tiktok.com/@{clean_user}"

    # Per actor docs, single-user runs should still fetch at least
    # a small follower list; we enable getFollowers with low maxItems.
    run_input = {
        "startUrls": [profile_url],
        "getFollowers": True,
        "getFollowing": False,
        "maxItems": 20,
    }

    try:
        logger.info(f"Fetching TikTok user profile for @{clean_user} using {actor_id}")
        run = client.actor(actor_id).call(run_input=run_input, timeout_secs=getattr(settings, "APIFY_TIMEOUT", 600))

        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            logger.warning("TikTok user scraper returned no datasetId")
            return None

        items = list(client.dataset(dataset_id).iterate_items())
        if not items:
            logger.warning("TikTok user scraper returned 0 items")
            return None

        # Prefer the item whose username matches our target; otherwise fallback to first.
        target = None
        for item in items:
            if item.get("username") and item.get("username").lower() == clean_user.lower():
                target = item
                break

        if not target:
            target = items[0]

        logger.info(
            f"TikTok user stats: username={target.get('username')}, followers={target.get('followers')}, likes={target.get('likes')}, videos={target.get('videos')}"
        )
        return target
    except Exception as e:
        logger.error(f"Failed to fetch TikTok user profile for {username}: {e}")
        return None


class ApifyScraperService(BaseScraperService):
    """
    Apify-based scraper service supporting multiple platforms.
    
    This service uses Apify actors to scrape data from various social media
    platforms in a unified way.
    """
    
    def __init__(self, platform: Platform, search_type: str = 'posts'):
        """
        Initialize Apify scraper.
        
        Args:
            platform: Target platform to scrape
            search_type: Search type ('posts', 'reels', etc.)
            
        Raises:
            ScraperException: If Apify is not configured
        """
        # Call base class init
        super().__init__(platform)
        self.search_type = search_type
        
        # Get Apify configuration
        self.api_token = getattr(settings, 'APIFY_API_TOKEN', '')
        if not self.api_token:
            raise ScraperException("APIFY_API_TOKEN not configured")
        
        # Initialize Apify client
        self.client = ApifyClient(self.api_token)
        
        # Get actor ID for platform / mode
        actors = getattr(settings, 'APIFY_ACTORS', {})
        # For TikTok, allow using a dedicated profile actor when search_type == 'profile'
        if platform == Platform.TIKTOK and search_type == 'profile':
            self.actor_id = actors.get('tiktok_profile', actors.get(platform.value, ''))
        else:
            self.actor_id = actors.get(platform.value, '')
        
        # Fallback: apidojo/tiktok-scraper (old) returns "Actor not found" without billing
        # apidojo/tiktok-scraper-api works; only fallback the old actor
        if platform == Platform.TIKTOK and self.actor_id == 'apidojo/tiktok-scraper':
            self.actor_id = 'clockworks/free-tiktok-scraper'
            self.logger.info("Using clockworks/free-tiktok-scraper (apidojo fallback)")
        
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
        """
        Build input for TikTok Apify actor.
        Supports: clockworks (searchQueries/hashtags/resultsPerPage),
        apidojo/tiktok-scraper, apidojo/tiktok-scraper-api (keywords/maxItems).
        """
        limit = min(max_results, self.max_results_limit)
        if "clockworks/free-tiktok-scraper" in (self.actor_id or ""):
            clean = keyword.strip().replace("#", "")
            if keyword.strip().startswith("#"):
                return {"hashtags": [clean], "resultsPerPage": limit}
            return {"searchQueries": [keyword], "resultsPerPage": limit}
        # apidojo (tiktok-scraper, tiktok-scraper-api)
        return {"keywords": [keyword], "maxItems": limit}
    
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
                "maxRequestRetries": 3,
                "maxComments": 0,
                "scrapeAbout": False,
                "scrapeReviews": False,
                "scrapePhotos": False,
                "scrapeVideos": False,
                # Proxy optimization: Ensure Apify Proxy is used effectively
                "proxyConfiguration": {
                    "useApifyProxy": True,
                    "apifyProxyGroups": ["RESIDENTIAL"] # Prefer residential to avoid detection
                },
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
                clean_user = username.replace("@", "").strip()
                limit = min(max_results, self.max_results_limit)
                if "clockworks/free-tiktok-scraper" in (self.actor_id or ""):
                    return {"profiles": [clean_user], "resultsPerPage": limit}
                # apidojo (tiktok-scraper, tiktok-scraper-api)
                profile_url = f"https://www.tiktok.com/@{clean_user}"
                return {"startUrls": [profile_url], "maxItems": limit}

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
                # ❌ FACEBOOK KEYWORD SEARCH BLOCKED
                # Tested 2026-02-05: Even with Residential Proxies, Facebook returns "Page access blocked"
                # for /search/ URLs without valid Cookies.
                #
                # Apify Actors support this ONLY if you provide valid 'cookies'.
                # Without cookies, we must restrict search to specific Pages/Users.
                
                raise ScraperException(
                    "❌ Facebook Search is blocked by Facebook for anonymous requests.\n"
                    "Even with Residential Proxies, Facebook requires Login (Cookies) to search globally.\n\n"
                    "✅ SOLUTION:\n"
                    "1. Use the 'User/Page' search tab (works better for public pages).\n"
                    "2. Or configure 'APIFY_FACEBOOK_COOKIES' in backend settings (advanced)."
                )
        
        elif self.platform == Platform.DOUYIN:
            # Similar to TikTok
            return self._build_tiktok_input(keyword, max_results)
        
        else:
            raise ScraperException(f"Unsupported platform: {self.platform}")
    
    def _get_related_hashtags(self, base: str) -> List[str]:
        """Tạo danh sách hashtag liên quan để mở rộng pool kết quả, tránh lặp 18 bài mỗi lần."""
        base = (base or '').strip().lower()
        if not base:
            return []
        seen = {base}
        result = [base]
        suffixes = ('dep', 'vang', 'bac', 'vietnam', 'ngoc', 'kimcuong', 'hot', 'trend')
        for s in suffixes:
            tag = base + s
            if tag not in seen and len(result) < 5:
                seen.add(tag)
                result.append(tag)
        return result

    def _caption_contains_keyword(self, post: Dict[str, Any], keyword: str) -> bool:
        """Kiểm tra post có caption chứa keyword (case-insensitive). Chấp nhận cả 'trang sức' và 'trangsuc'. Facebook dùng 'text'."""
        if not post or not isinstance(post, dict):
            return False
        caption = post.get('caption', '') or post.get('description', '') or post.get('text', '') or ''
        if not caption:
            return False
        kw = (keyword or '').strip()
        if not kw:
            return True
        cap_lower = str(caption).lower()
        if kw.lower() in cap_lower:
            return True
        kw_no_space = kw.replace(' ', '').lower()
        if kw_no_space and kw_no_space in cap_lower.replace(' ', ''):
            return True
        return False

    def _sort_by_caption_relevance(self, results: List[Dict], keyword: str) -> List[Dict]:
        """Sắp xếp: bài có caption chứa keyword lên trước."""
        kw = (keyword or '').strip().lower()
        if not kw:
            return results

        def score(r):
            cap = ((r.get('caption') or r.get('description') or '') if isinstance(r, dict) else '')
            return 0 if kw in str(cap).lower() else 1

        return sorted(results, key=score)

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
        min_comments: int = 0,
        max_results: int = 20,
        search_mode: str = "hashtag"
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
            use_keyword_mode = False  # Set by platform-specific blocks that need caption filter
            # Facebook Reels: keyword search via facebook-video-search-scraper (trả về reels + videos)
            if (self.platform == Platform.FACEBOOK and self.search_type == 'reels'
                    and not keyword.startswith('@') and not keyword.startswith('http')
                    and 'facebook.com' not in keyword.lower()):
                import urllib.parse
                actors_fb = getattr(settings, 'APIFY_ACTORS', {})
                self.actor_id = actors_fb.get('facebook_video_search', 'apify/facebook-video-search-scraper')
                search_query = keyword.replace('#', '').strip()
                limit_capped = min(max_results, self.max_results_limit)
                search_url = f"https://www.facebook.com/watch/search?q={urllib.parse.quote(search_query)}"
                actor_input = {
                    "startUrls": [{"url": search_url}],
                    "searchTerm": search_query,
                    "resultsLimit": limit_capped,
                }
                self.logger.info(f"Using Facebook Video Search (reels) for '{search_query}' - limit={limit_capped}")
            # Facebook Posts: hashtag + caption (hybrid like Instagram)
            elif (self.platform == Platform.FACEBOOK and not keyword.startswith('@')
                    and not keyword.startswith('http') and 'facebook.com' not in keyword.lower()):
                import unicodedata
                actors_fb = getattr(settings, 'APIFY_ACTORS', {})
                self.actor_id = actors_fb.get('facebook_hashtag', 'apify/facebook-hashtag-scraper')
                search_mode_val = (search_mode or "hashtag").strip().lower()
                use_keyword_mode = search_mode_val == "keyword"
                search_query = keyword.replace('#', '').strip()
                clean_keyword = search_query.replace(' ', '').lower()
                normalized = unicodedata.normalize('NFD', clean_keyword)
                clean_keyword = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
                if not clean_keyword:
                    clean_keyword = search_query.replace(' ', '')
                limit_capped = min(max_results, self.max_results_limit)
                if use_keyword_mode:
                    hashtags_list = self._get_related_hashtags(clean_keyword)[:2]
                    limit_per_tag = min(50, max(3, (limit_capped * 2) // max(1, len(hashtags_list))))
                    actor_input = {
                        "keywordList": hashtags_list,
                        "resultsLimit": limit_per_tag,
                    }
                    self.logger.info(f"Using Facebook Hybrid (keyword) for '{search_query}' - hashtags={hashtags_list}, limit={limit_per_tag}/tag, will filter by caption")
                else:
                    actor_input = {
                        "keywordList": [clean_keyword],
                        "resultsLimit": limit_capped,
                    }
                    self.logger.info(f"Using Facebook Hashtag Scraper for #{clean_keyword} - limit={limit_capped}")
            # Instagram: hashtag vs keyword (different actors for different results)
            elif self.platform == Platform.INSTAGRAM and not keyword.startswith('@') and not keyword.startswith('http'):
                import unicodedata
                search_mode_val = (search_mode or "hashtag").strip().lower()
                use_keyword_mode = search_mode_val == "keyword"

                # Luôn chỉ fetch đúng max_results (30) - không tăng để tránh tốn token Apify
                limit_capped = min(max_results, self.max_results_limit)
                results_type = "reels" if self.search_type == "reels" else "posts"

                # Keyword mode: Hybrid = hashtag scraper + filter caption chứa keyword
                # Dùng 2 hashtag liên quan để lấy pool rộng hơn, sau đó filter theo caption
                # Hashtag mode: 1 hashtag chính
                self.actor_id = 'apify/instagram-hashtag-scraper'
                search_query = keyword.replace('#', '').strip()
                clean_keyword = search_query.replace(' ', '').strip()
                normalized = unicodedata.normalize('NFD', clean_keyword)
                clean_keyword = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
                if not clean_keyword:
                    clean_keyword = search_query.replace(' ', '')
                if use_keyword_mode:
                    hashtags_list = self._get_related_hashtags(clean_keyword)[:2]
                    # Không floor 15 - dùng đúng limit_capped để tiết kiệm token Apify
                    limit_per_tag = min(30, max(3, (limit_capped * 2) // len(hashtags_list)))
                    actor_input = {
                        "hashtags": hashtags_list,
                        "resultsType": results_type,
                        "resultsLimit": limit_per_tag,
                        "searchLimit": limit_per_tag,
                        "searchType": "hashtag",
                    }
                    self.logger.info(f"Using Instagram Hybrid (keyword) for '{search_query}' - hashtags={hashtags_list}, limit={limit_per_tag}/tag, will filter by caption")
                else:
                    hashtags_list = [clean_keyword]
                    actor_input = {
                        "hashtags": hashtags_list,
                        "resultsType": results_type,
                        "resultsLimit": limit_capped,
                        "searchLimit": limit_capped,
                        "searchType": "hashtag",
                    }
                    self.logger.info(f"Using Instagram Hashtag Scraper for #{clean_keyword} - limit={limit_capped}")
            else:
                # Default input builder
                actor_input = self._build_actor_input(keyword, max_results)
                
            results = self.run_actor(actor_input)

            # apify/instagram-scraper có thể trả hashtag metadata (name, posts, topPosts, latestPosts) - flatten lấy post thật
            if self.actor_id == 'apify/instagram-scraper' and results:
                flat = []
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    if 'topPosts' in item or 'latestPosts' in item:
                        for p in item.get('topPosts', []) + item.get('latestPosts', []):
                            if isinstance(p, dict) and (p.get('shortCode') or p.get('id') or p.get('displayUrl')):
                                flat.append(p)
                    elif 'posts' in item:
                        for p in item.get('posts', []):
                            if isinstance(p, dict) and (p.get('shortCode') or p.get('id') or p.get('displayUrl')):
                                flat.append(p)
                    elif item.get('shortCode'):
                        flat.append(item)
                if flat:
                    results = flat
                    self.logger.info(f"Flattened instagram-scraper results: {len(results)} posts")
            
            # Facebook: dedupe (có thể trùng khi dùng nhiều hashtag) và shuffle
            if self.platform == Platform.FACEBOOK and results:
                seen_ids = set()
                deduped = []
                for r in results:
                    if isinstance(r, dict):
                        pid = r.get('postId') or r.get('id', '')
                        if not pid and r.get('video'):
                            pid = r.get('video', {}).get('id', '')
                        if not pid and r.get('videoUrl'):
                            pid = r.get('videoUrl', '')
                        if pid and pid not in seen_ids:
                            seen_ids.add(pid)
                            deduped.append(r)
                if deduped:
                    results = deduped
                    random.shuffle(results)
                    self.logger.info(f"Deduped to {len(results)} unique Facebook posts, shuffled")

            # Facebook KEYWORD mode: filter theo caption (text) chứa keyword
            if self.platform == Platform.FACEBOOK and results and use_keyword_mode:
                search_query = keyword.replace('#', '').strip()
                before_count = len(results)
                results_before_caption = list(results)
                results = [r for r in results if isinstance(r, dict) and self._caption_contains_keyword(r, search_query)]
                self.logger.info(f"Facebook caption filter: {before_count} -> {len(results)} posts (keyword='{search_query}')")
                if len(results) < before_count * 0.3 and before_count >= 10:
                    self.logger.info("Caption filter quá chặt, giữ thêm bài không khớp caption")
                    results = self._sort_by_caption_relevance(results_before_caption, search_query)

            # Facebook: truncate
            if self.platform == Platform.FACEBOOK and len(results) > max_results:
                results = results[:max_results]
                self.logger.info(f"Truncated to {max_results} Facebook posts")

            # Instagram: dedupe (có thể trùng khi dùng nhiều hashtag) và shuffle
            if self.platform == Platform.INSTAGRAM and results:
                seen_ids = set()
                deduped = []
                for r in results:
                    if isinstance(r, dict):
                        vid = r.get('id') or r.get('shortCode', '')
                        if vid and vid not in seen_ids:
                            seen_ids.add(vid)
                            deduped.append(r)
                if deduped:
                    results = deduped
                    random.shuffle(results)
                    self.logger.info(f"Deduped to {len(results)} unique posts, shuffled for variety")
            
            # KEYWORD mode (hybrid): filter theo caption chứa keyword
            if self.platform == Platform.INSTAGRAM and results and use_keyword_mode:
                search_query = keyword.replace('#', '').strip()
                before_count = len(results)
                results_before_caption = list(results)
                results = [r for r in results if isinstance(r, dict) and self._caption_contains_keyword(r, search_query)]
                self.logger.info(f"Caption filter: {before_count} -> {len(results)} posts (keyword='{search_query}')")
                if len(results) < before_count * 0.3 and before_count >= 10:
                    self.logger.info("Caption filter quá chặt, giữ thêm bài không khớp caption để đủ kết quả")
                    results = self._sort_by_caption_relevance(results_before_caption, search_query)
            
            # Chỉ trả tối đa max_results (30) để tránh tốn token Apify
            if self.platform == Platform.INSTAGRAM and len(results) > max_results:
                results = results[:max_results]
                self.logger.info(f"Truncated to {max_results} videos (Apify limit)")
            self.logger.info(f"Found {len(results)} videos for keyword: {keyword}")
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
        """
        Normalize TikTok/Douyin data.

        Supports both the legacy free-tiktok-scraper format and the
        apidojo/tiktok-scraper output schema.
        """
        # Video ID
        video_id = data.get("id", "") or data.get("video_id", "")

        # Author / channel info - support multiple formats
        channel = data.get("channel", {}) or {}
        author_meta = (
            channel
            or data.get("authorMeta", {})
            or data.get("author", {})
        )

        author_username = (
            author_meta.get("username")
            or author_meta.get("name", "")
            or author_meta.get("uniqueId", "")
            or author_meta.get("unique_id", "")
        )
        author_name = (
            author_meta.get("nickname")
            or author_meta.get("nickName")
            or author_meta.get("name")
            or author_meta.get("display_name")
            or author_username
        )

        # Stats - apidojo, clockworks (flat), legacy stats objects
        stats = data.get("stats", {}) or data.get("statistics", {})
        likes_count = (
            data.get("likes")
            or data.get("diggCount")
            or stats.get("diggCount")
            or stats.get("digg_count", 0)
        )
        views_count = (
            data.get("views")
            or data.get("playCount")
            or stats.get("playCount")
            or stats.get("play_count", 0)
        )
        comments_count = (
            data.get("comments")
            or data.get("commentCount")
            or stats.get("commentCount")
            or stats.get("comment_count", 0)
        )
        shares_count = (
            data.get("shares")
            or data.get("shareCount")
            or stats.get("shareCount")
            or stats.get("share_count", 0)
        )

        # URLs
        video_url = (
            data.get("postPage")
            or data.get("webVideoUrl", "")
        )
        if not video_url and author_username and video_id:
            video_url = f"https://www.tiktok.com/@{author_username}/video/{video_id}"

        # Download / media URLs
        video_meta = data.get("videoMeta", {}) or data.get("video", {})
        download_url = ""
        if isinstance(video_meta, dict):
            download_url = (
                video_meta.get("url", "")
                or video_meta.get("downloadAddr", "")
                or video_meta.get("playAddr", "")
                or video_meta.get("play_addr", "")
            )

        if not download_url:
            media_urls = data.get("mediaUrls", [])
            if media_urls and isinstance(media_urls, list):
                download_url = media_urls[0]

        # Thumbnail / cover
        thumbnail_url = ""
        if isinstance(video_meta, dict):
            thumbnail_url = (
                video_meta.get("thumbnail")
                or video_meta.get("cover")
                or video_meta.get("coverUrl")
                or video_meta.get("dynamicCover")
                or video_meta.get("originCover")
            )

        # Music info
        music_meta = data.get("musicMeta", {}) or data.get("song", {})
        music_title = (
            music_meta.get("musicName")
            or music_meta.get("title", "")
        )
        music_author = (
            music_meta.get("musicAuthor")
            or music_meta.get("artist", "")
        )

        # Duration extraction
        duration = (
            data.get("videoMeta", {}).get("duration")
            or video_meta.get("duration", 0)
        )

        # Published time
        create_time = (
            data.get("uploadedAt")
            or data.get("uploadedAtFormatted")
            or data.get("createTime")
            or data.get("createTimeISO")
        )

        # Hashtags
        hashtags = data.get("hashtags") or []
        if hashtags and isinstance(hashtags, list) and isinstance(hashtags[0], dict):
            hashtags = [tag.get("name", "") for tag in hashtags]

        return {
            "video_id": str(video_id),
            "duration": duration,
            "title": data.get("title") or data.get("text", ""),
            "description": data.get("text", "") or data.get("title", ""),
            "author_username": author_username,
            "author_name": author_name,
            "likes_count": int(likes_count) if likes_count else 0,
            "views_count": int(views_count) if views_count else 0,
            "comments_count": int(comments_count) if comments_count else 0,
            "shares_count": int(shares_count) if shares_count else 0,
            "video_url": video_url,
            "download_url": download_url,
            "thumbnail_url": thumbnail_url,
            "published_at": self._parse_timestamp(create_time),
            "hashtags": hashtags,
            "music_info": {
                "title": music_title,
                "author": music_author,
                "url": "",
            },
            "raw_data": data,
        }
    
    def _normalize_instagram_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Instagram data (hashtag scraper + crawlerbros keyword scraper)."""
        if not isinstance(data, dict):
            self.logger.warning(f"Skipping non-dict Instagram item: {type(data)}")
            return {}
        # crawlerbros/instagram-keyword-search-scraper format mapping
        if data.get('post_id') or data.get('post_url'):
            data = dict(data)
            data.setdefault('id', data.get('post_id'))
            data.setdefault('shortCode', data.get('post_id'))
            data.setdefault('url', data.get('post_url'))
            data.setdefault('postUrl', data.get('post_url'))
            data.setdefault('ownerUsername', data.get('username'))
            data.setdefault('displayUrl', data.get('thumbnail_url'))
            data.setdefault('thumbnailUrl', data.get('thumbnail_url'))
            urls = data.get('media_urls') or []
            if data.get('media_type') == 'video' and urls:
                data.setdefault('videoUrl', urls[0] if isinstance(urls[0], str) else urls[0].get('url', ''))
            data.setdefault('likesCount', data.get('likes_count', 0))
            data.setdefault('commentsCount', data.get('comments_count', 0))
            data.setdefault('videoViewCount', data.get('views_count', 0))
        self.logger.debug(f"Normalizing Instagram Data. Keys: {list(data.keys())}")
        
        # Thumbnail fallback logic - Apify Instagram có thể trả về nhiều format khác nhau
        thumbnail = ''
        if data.get('images') and len(data['images']) > 0:
            thumb = data['images'][0]
            if isinstance(thumb, str) and thumb:
                thumbnail = thumb
            elif isinstance(thumb, dict) and thumb:
                thumbnail = thumb.get('url') or thumb.get('src') or ''
        if not thumbnail:
            thumbnail = (
                data.get('displayUrl') or
                data.get('thumbnailUrl') or
                data.get('url')
            )
        # Carousel/có childPosts: lấy thumbnail từ slide đầu
        if not thumbnail and data.get('childPosts'):
            for c in data.get('childPosts', []) or []:
                if isinstance(c, dict):
                    t = c.get('displayUrl') or (c.get('images', []) or [None])[0]
                    if t and isinstance(t, str):
                        thumbnail = t
                        break
        # displayResources (array of quality URLs)
        if not thumbnail and data.get('displayResources'):
            res = data['displayResources']
            if isinstance(res, list) and res:
                first = res[0]
                if isinstance(first, dict) and first.get('src'):
                    thumbnail = first['src']
                elif isinstance(first, str):
                    thumbnail = first

        # Author fallback logic
        author_username = data.get('ownerUsername', '')
        if not author_username:
            # Try nested objects
            if data.get('owner'):
                author_username = data['owner'].get('username', '')
            elif data.get('user'):
                 author_username = data['user'].get('username', '')
        
        # If still no username, try to extract from 'author' field if it exists
        if not author_username:
            author_username = data.get('author', '')

        # Normalize stats (ensure int)
        likes = data.get('likesCount') or data.get('likes', 0)
        views = data.get('videoViewCount') or data.get('videoPlayCount') or data.get('views', 0)
        comments = data.get('commentsCount') or data.get('comments', 0)
        
        # DEBUG: Log raw stats to understand why likes might be 0
        self.logger.debug(f"Instagram post stats - likesCount: {data.get('likesCount')}, likes: {data.get('likes')}, "
                         f"videoViewCount: {data.get('videoViewCount')}, commentsCount: {data.get('commentsCount')}")

        # Timestamp logic
        timestamp_raw = (
            data.get('timestamp') or 
            data.get('date') or 
            data.get('takenAt') or 
            data.get('taken_at_timestamp')
        )

        return {
            'video_id': data.get('id') or data.get('shortCode', ''),
            'title': data.get('caption', ''),
            'description': data.get('caption', ''),
            'author_username': author_username or 'unknown',
            'author_name': data.get('ownerFullName') or data.get('fullName') or author_username,
            'likes_count': int(likes) if likes else 0,
            'views_count': int(views) if views else 0,
            'comments_count': int(comments) if comments else 0,
            'shares_count': 0,  # Instagram doesn't provide this
            'video_url': data.get('url') or data.get('postUrl', f"https://www.instagram.com/p/{data.get('shortCode')}/") if data.get('shortCode') else '',
            'download_url': data.get('videoUrl', ''),
            'thumbnail_url': thumbnail,
            'published_at': self._parse_timestamp(timestamp_raw),
            'hashtags': data.get('hashtags', []),
            'music_info': {},
            'raw_data': data
        }
    
    def _normalize_facebook_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Facebook data (posts-scraper, hashtag-scraper, video-search-scraper)."""

        def _extract_url(val: Any) -> str:
            """Extract URL string from value; Apify may return dict e.g. {uri: '...'}."""
            if not val:
                return ''
            if isinstance(val, str):
                return val
            if isinstance(val, dict):
                return val.get('url') or val.get('uri') or val.get('src') or val.get('link') or ''
            return str(val)

        # Clean ID (video-search-scraper uses video.id)
        post_id = data.get('postId') or data.get('id', '')
        if not post_id and data.get('video'):
            post_id = data.get('video', {}).get('id', '')
        
        # Stats (hashtag-scraper uses likesCount/commentsCount/sharesCount)
        likes = data.get('likes') or data.get('likesCount', 0)
        comments = data.get('comments') or data.get('commentsCount', 0)
        shares = data.get('shares') or data.get('sharesCount', 0)
        
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
        
        # Fallback for flat structure or missing user object (hashtag-scraper uses pageName)
        if not author_username:
             author_username = data.get('postAuthor', '') or data.get('pageName', '')
        if not author_name:
             author_name = data.get('postAuthor', '') or data.get('pageName', '')
        # video-search-scraper uses video_owner_profile: {name, url, uri_token, id}
        owner_profile = data.get('video_owner_profile', {})
        if isinstance(owner_profile, dict):
            if not author_name:
                author_name = owner_profile.get('name', '')
            if not author_username:
                author_username = owner_profile.get('uri_token', '') or owner_profile.get('url', '').split('facebook.com/')[-1].split('/')[0] if owner_profile.get('url') else owner_profile.get('id', '')
             
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

        # Robust ID Extraction
        if not post_id:
            # Try to extract from URL if API didn't return ID
            import re
            url_to_check = _extract_url(data.get('url') or data.get('postUrl') or data.get('videoUrl', ''))
            
            # Match patterns: /posts/123, /videos/123, /reel/123, ?fbid=123, /photo.php?fbid=123
            id_patterns = [
                r'/posts/(\d+)',
                r'/videos/(\d+)',
                r'/reel/(\d+)',
                r'fbid=(\d+)',
                r'/photos/.*?/(\d+)',
                r'story_fbid=(\d+)',
                r'/(\d+)/?$' # Numeric ID at end of path
            ]
            
            for pattern in id_patterns:
                match = re.search(pattern, url_to_check)
                if match:
                    post_id = match.group(1)
                    break
            
            # If still no ID but we have URL, create a hash ID to prevent skipping
            if not post_id and url_to_check:
                import hashlib
                post_id = f"hash_{hashlib.md5(url_to_check.encode()).hexdigest()[:16]}"
                logger.info(f"⚠️ Generated hash ID for post: {post_id} from {url_to_check}")

        # Media (Video/Image)
        video_url = _extract_url(data.get('videoUrl'))
        
        # THUMBNAIL EXTRACTION STRATEGY
        # 1. Direct keys (may be dict from Apify)
        thumbnail_url = _extract_url(
            data.get('image') or 
            data.get('imageUrl') or 
            data.get('thumbnail') or 
            data.get('fullImage') or
            ''
        )

        # 2. Images list (often contains high-res photo URL)
        if not thumbnail_url and data.get('images') and isinstance(data.get('images'), list):
            if len(data['images']) > 0:
                thumbnail_url = _extract_url(data['images'][0]) if data['images'] else ''

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
        # 5. video-search-scraper uses thumbnail_image.uri
        if not thumbnail_url and data.get('thumbnail_image'):
            thumb_img = data['thumbnail_image']
            if isinstance(thumb_img, dict) and thumb_img.get('uri'):
                thumbnail_url = thumb_img['uri']
        
        # Construct Web URL (Permalink) - MUST BE BEFORE is_video detection (video-search uses videoUrl)
        permalink = _extract_url(data.get('url') or data.get('postUrl') or data.get('videoUrl'))
        
        # Determine if it's a video
        # Apify fb scraper often puts isVideo=True at root
        is_video = data.get('isVideo', False)

        # Detect from URL if it's a Reel
        if not is_video and permalink and ('/reel/' in permalink or '/videos/' in permalink):
            is_video = True
            
        # Detect if videoUrl exists at root
        if not is_video and (data.get('videoUrl') or data.get('video_url')):
            is_video = True
            video_url = _extract_url(data.get('videoUrl') or data.get('video_url')) or video_url

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
            'title': data.get('text', '') or data.get('message', '') or data.get('title', '') or 'No Content',
            'description': data.get('text', '') or data.get('message', '') or data.get('save_description', ''),
            'author_username': str(author_username),
            'author_name': str(author_name),
            'likes_count': int(likes) if isinstance(likes, (int, float, str)) and str(likes).isdigit() else 0,
            'views_count': int(data.get('views') or data.get('viewCount') or data.get('videoViewCount') or 0),
            'comments_count': int(comments) if isinstance(comments, (int, float, str)) and str(comments).isdigit() else 0,
            'shares_count': int(shares) if isinstance(shares, (int, float, str)) and str(shares).isdigit() else 0,
            'video_url': permalink, # Normalized Web URL
            'download_url': video_url, # Direct file URL
            'thumbnail_url': thumbnail_url,
            'published_at': self._parse_timestamp(data.get('time') or data.get('timestamp') or data.get('date')),
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
                return datetime.fromtimestamp(timestamp, timezone.utc)
            
            # If ISO string
            if isinstance(timestamp, str):
                timestamp_str = timestamp.strip()
                
                # Check if string contains only digits (Unix timestamp as string)
                if timestamp_str.isdigit():
                    return datetime.fromtimestamp(int(timestamp_str), timezone.utc)
                
                # Replace Z with +00:00 for ISO format
                timestamp_str = timestamp_str.replace('Z', '+00:00')
                
                try:
                    # Try ISO format with timezone
                    dt = datetime.fromisoformat(timestamp_str)
                    # Ensure timezone aware
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                except:
                    # Try without microseconds
                    try:
                        if '.' in timestamp_str:
                            timestamp_str = timestamp_str.split('.')[0]
                        return datetime.fromisoformat(timestamp_str)
                    except:
                        # Try common formats
                        for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S']:
                            try:
                                return datetime.strptime(timestamp_str.split('+')[0].split('Z')[0], fmt)
                            except:
                                continue
            
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
        
        # Note: Instagram Reels functionality is handled by ApifyScraperService
        # using the _build_instagram_reels_input method - now passing search_type to handle it properly
        return ApifyScraperService(platform_enum, search_type=search_type)
        
    except KeyError:
        raise ValueError(
            f"Unsupported platform: {platform}. "
            f"Supported: {', '.join([p.value for p in Platform])}"
        )
