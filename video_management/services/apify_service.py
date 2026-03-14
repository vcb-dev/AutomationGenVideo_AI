"""
Apify scraper service for all platforms.

This service uses Apify actors to scrape data from TikTok, Instagram,
Facebook, and Douyin.
"""

import logging
import random
import time
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


def normalize_username(username_or_url: str) -> str:
    """
    Extract a clean username from a potential URL or @handle.
    Supports TikTok, Instagram, Facebook, and Douyin URLs.
    """
    if not username_or_url:
        return ""
        
    # Remove leading/trailing spaces and leading @
    clean = username_or_url.strip().lstrip('@')
    
    # Handle common URL patterns
    lower_clean = clean.lower()
    if any(domain in lower_clean for domain in ['tiktok.com', 'instagram.com', 'facebook.com', 'douyin.com']):
        # Remove query parameters and fragments
        clean = clean.split('?')[0].split('#')[0]
        # Remove trailing slash
        clean = clean.rstrip('/')
        # Split by slash
        parts = clean.split('/')
        
        # Douyin has /user/ID
        if 'douyin.com' in lower_clean:
            for i, part in enumerate(parts):
                if part == 'user' and i + 1 < len(parts):
                    return parts[i+1]
        
        # General strategy: find part starting with @ or take the last non-empty part
        # Most platforms use /@username or /username
        for part in reversed(parts):
            if part.startswith('@'):
                return part.replace('@', '')
            # If it's a known non-username part, skip
            if part.lower() in [
                'www.tiktok.com', 'tiktok.com', 'www.instagram.com', 'instagram.com', 
                'facebook.com', 'www.facebook.com', 'explore', 'reels', 'p', 'user', 'video', 'watch'
            ]:
                continue
            if part:
                return part
                
    # If not a URL, just remove any remaining @
    return clean.replace("@", "")


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
    clean_user = normalize_username(username)
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
        max_results: int = 20,
        search_mode: str = "hashtag"
    ) -> Dict[str, Any]:
        """
        Build input for TikTok Apify actor.
        
        Khi search_mode='keyword', fetch pool lớn hơn (x3) để sau khi lọc
        caption/hashtag còn đủ kết quả chất lượng.
        """
        # Fetch pool vừa đủ (max 35) để tiết kiệm token và có 5 video buffer cho Random Sample
        pool_size = min(max_results + 5, 35)

        clean = keyword.strip().replace("#", "")

        if "clockworks/free-tiktok-scraper" in (self.actor_id or ""):
            if keyword.strip().startswith("#") or search_mode == "hashtag":
                return {"hashtags": [clean], "resultsPerPage": pool_size}
            # Keyword mode: dùng searchQueries để TikTok search theo từ khóa
            return {"searchQueries": [keyword.strip()], "resultsPerPage": pool_size}
        # apidojo (tiktok-scraper, tiktok-scraper-api)
        return {"keywords": [keyword.strip()], "maxItems": pool_size}

    
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
            # Clean username using robust normalizer
            clean_user = normalize_username(username)
            
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
        until_date: Optional[str] = None,
        search_mode: str = "hashtag"
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
                clean_user = normalize_username(username)
                limit = min(max_results, self.max_results_limit)
                if "clockworks/free-tiktok-scraper" in (self.actor_id or ""):
                    return {"profiles": [clean_user], "resultsPerPage": limit}
                # apidojo (tiktok-scraper, tiktok-scraper-api)
                profile_url = f"https://www.tiktok.com/@{clean_user}"
                return {"startUrls": [profile_url], "maxItems": limit}

            return self._build_tiktok_input(keyword, max_results, search_mode)
        
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
            return self._build_tiktok_input(keyword, max_results, search_mode)
        
        elif self.platform == Platform.XIAOHONGSHU:
            # Xiaohongshu input using kuaima/xiaohongshu-search format
            return {
                "keywords": [username or keyword],
                "sortType": "general",
                "maxItems": max_results,
                "proxyConfig": { "useApifyProxy": True }
            }
        
        else:
            raise ScraperException(f"Unsupported platform: {self.platform}")
    
    def _sanitize_hashtag(self, tag: str) -> str:
        """
        Sanitize a hashtag to comply with Apify actor regex:
        ^[^!?.,:;\-+=*&%$#@/\~^|<>()[\]{}'"\`\s]+$
        → Remove #, spaces, and any other forbidden special characters.
        """
        import re
        # Remove leading # and whitespace
        tag = (tag or '').strip().lstrip('#')
        # Remove all characters that are NOT alphanumeric, underscore, or unicode letters/digits
        tag = re.sub(r'[!?.,:;\-+=*&%$#@/\~^|<>()\[\]{}\'"\`\s]', '', tag)
        return tag.strip()

    def _get_related_terms(self, base: str) -> List[str]:
        """Tạo danh sách từ khóa/hashtag liên quan để mở rộng pool kết quả, tránh lặp bài mỗi lần."""
        base = (base or '').strip().lower()
        if not base:
            return []
        
        # Loại bỏ dấu # nếu có để xử lý text thuần
        clean_base = self._sanitize_hashtag(base)
        if not clean_base:
            return []
        
        seen = {clean_base}
        result = [clean_base]
        
        # Các hậu tố đa dạng để mở rộng pool (hashtag style - KHÔNG có space)
        suffixes = ['dep', 'vang', 'bac', 'hot', 'trend', '2025', 'style', 'viral']
        
        pool_suffixes = list(suffixes)
        random.shuffle(pool_suffixes)
        
        for s in pool_suffixes:
            # Luôn viết liền (hashtag không có dấu cách)
            term_no_space = clean_base + s
            
            if term_no_space not in seen and len(result) < 4:
                seen.add(term_no_space)
                result.append(term_no_space)
                
        return result

    def _is_facebook_group_post(self, data: Dict[str, Any]) -> bool:
        """Detect nếu post từ hội nhóm (group) cần loại khi muốn chỉ shop/page."""
        if not data or not isinstance(data, dict):
            return False
        url = data.get('url') or data.get('postUrl') or ''
        if isinstance(url, dict):
            url = url.get('url') or url.get('uri') or ''
        url = str(url).lower()
        return '/groups/' in url or '/group/' in url

    def _is_facebook_reel_or_video(self, data: Dict[str, Any]) -> bool:
        """Detect if raw Facebook item là reel/video cần loại khi search_type=posts."""
        if not data or not isinstance(data, dict):
            return False
        atts = data.get('attachments') or []
        has_photo = any(isinstance(a, dict) and str(a.get('type', '')).lower() in ('photo', 'image') for a in atts)
        # Post có ảnh sản phẩm (photo/image) → giữ lại dù có video (ảnh + caption là chính)
        if has_photo:
            return False
        # isVideo at root
        if data.get('isVideo', False):
            return True
        # URL chứa /reel/ hoặc /videos/ → reel/video thuần
        url = data.get('url') or data.get('postUrl') or ''
        if isinstance(url, dict):
            url = url.get('url') or url.get('uri') or ''
        url = str(url)
        if '/reel/' in url or '/videos/' in url:
            return True
        # videoUrl at root VÀ không có photo → video post
        if (data.get('videoUrl') or data.get('video_url')) and not has_photo:
            return True
        # Attachments CHỈ có video (không có photo/image)
        if atts:
            types = [str(a.get('type', '')).lower() for a in atts if isinstance(a, dict) and a.get('type')]
            if types and all(t in ('video', 'reel') for t in types):
                return True
        return False

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

    def _compute_trend_score(self, item: dict, platform: str = 'tiktok') -> float:
        """
        Tính điểm xu hướng = engagement × recency_multiplier.
        Video viral + gần đây → điểm cao → ưu tiên hiển thị liên tục.

        Recency bonus:
          <= 7 ngày  : 2.0×  (xu hướng hiện nay)    
          <= 30 ngày : 1.5×  (mới trong tháng)
          <= 90 ngày : 1.2×  (vẫn còn mới)
          > 90 ngày  : 0.8×  (cũ, giảm nhẹ)
        """
        import datetime

        # --- Engagement ---
        if platform == 'instagram':
            likes = item.get('likesCount') or item.get('likes') or 0
            views = item.get('videoViewCount') or item.get('videoPlayCount') or item.get('views') or 0
            comments = item.get('commentsCount') or item.get('comments') or 0
        else:  # tiktok / default
            likes = item.get('diggCount') or item.get('likes') or item.get('heart') or 0
            views = item.get('playCount') or item.get('views') or item.get('play_count') or 0
            comments = item.get('commentCount') or item.get('comments') or item.get('comment_count') or 0
        try:
            likes = int(likes) if likes else 0
            views = int(views) if views else 0
            comments = int(comments) if comments else 0
        except (TypeError, ValueError):
            likes = views = comments = 0

        engagement = likes + (views * 0.05) + (comments * 3)

        # --- Recency ---
        recency_mult = 1.0
        date_candidates = [
            item.get('createTimestamp'), item.get('createTime'),
            item.get('timestamp'), item.get('takenAtTimestamp'),
            item.get('publishedAt'), item.get('published_at'),
            item.get('date'),
        ]
        for raw in date_candidates:
            if not raw:
                continue
            try:
                if isinstance(raw, (int, float)) and raw > 1_000_000:
                    published = datetime.datetime.fromtimestamp(float(raw))
                elif isinstance(raw, str):
                    published = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
                    if published.tzinfo:
                        published = published.replace(tzinfo=None)
                else:
                    continue
                days_ago = (datetime.datetime.now() - published).days
                if days_ago <= 7:
                    recency_mult = 2.0
                elif days_ago <= 30:
                    recency_mult = 1.5
                elif days_ago <= 90:
                    recency_mult = 1.2
                else:
                    recency_mult = 0.8
                break  # dùng giá trị đầu tiên hợp lệ
            except Exception:
                continue

        return engagement * recency_mult

    def _tiktok_is_relevant(self, data: Dict[str, Any], keyword: str) -> bool:
        """
        Kiểm tra video TikTok có liên quan đến keyword không.
        Tìm trong: description (text), title, hashtags.
        Hỗ trợ tiếng Việt (có/không dấu) và chuẩn hóa unicode.
        """
        if not data or not isinstance(data, dict):
            return False
        kw = (keyword or '').strip()
        if not kw:
            return True  # Không có keyword -> giữ hết

        import unicodedata as _ud

        def _norm(s: str) -> str:
            s = s.lower().strip()
            return ''.join(c for c in _ud.normalize('NFD', s) if _ud.category(c) != 'Mn')

        kw_lower = kw.lower()
        kw_norm = _norm(kw)
        kw_no_space = kw_lower.replace(' ', '')
        kw_norm_no_space = kw_norm.replace(' ', '')

        def _text_matches(text: str) -> bool:
            if not text:
                return False
            t = str(text)
            t_lower = t.lower()
            t_norm = _norm(t)
            if kw_lower in t_lower:
                return True
            if kw_norm and kw_norm in t_norm:
                return True
            if kw_no_space and kw_no_space in t_lower.replace(' ', ''):
                return True
            if kw_norm_no_space and kw_norm_no_space in t_norm.replace(' ', ''):
                return True
            return False

        # Kiểm tra description / text (trường chính của TikTok)
        desc = data.get('text', '') or data.get('description', '') or data.get('title', '') or ''
        if _text_matches(desc):
            return True

        # Kiểm tra hashtags (list of str hoặc list of dict)
        hashtags = data.get('hashtags', []) or []
        for tag in hashtags:
            tag_name = tag.get('name', '') if isinstance(tag, dict) else str(tag)
            if _text_matches(tag_name):
                return True

        return False

    def _tiktok_sort_by_relevance(self, results: List[Dict], keyword: str) -> List[Dict]:
        """
        Sắp xếp TikTok results: video liên quan đến keyword lên trước.
        """
        kw = (keyword or '').strip().lower()
        if not kw:
            return results

        def _relevance_score(r):
            if not isinstance(r, dict):
                return 99
            desc = (r.get('text', '') or r.get('description', '') or r.get('title', '') or '').lower()
            hashtags = r.get('hashtags', []) or []
            tag_text = ' '.join(
                t.get('name', '') if isinstance(t, dict) else str(t)
                for t in hashtags
            ).lower()
            full_text = f"{desc} {tag_text}"
            if kw in full_text:
                return -full_text.count(kw)  # Càng nhiều lần xuất hiện -> điểm càng thấp (sort ascending)
            return 99  # Không khớp -> xuống cuối

        return sorted(results, key=_relevance_score)

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
        search_mode: str = "hashtag",
        session_id: str = None
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
            results = None  # Set by FB posts dual-run, else from run_actor below
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
            # Facebook Posts: scraper_one/facebook-posts-search (query, resultsCount)
            elif (self.platform == Platform.FACEBOOK and not keyword.startswith('@')
                    and not keyword.startswith('http') and 'facebook.com' not in keyword.lower()):
                actors_fb = getattr(settings, 'APIFY_ACTORS', {})
                self.actor_id = actors_fb.get('facebook_posts_search', 'scraper_one/facebook-posts-search')
                search_mode_val = (search_mode or 'hashtag').strip().lower()
                clean_kw = keyword.replace('#', '').strip()
                # Hashtag: dùng #trangsuc, không thêm "shop" (tìm đúng hashtag)
                # Keyword: thêm "shop" để bias shop (không hội nhóm)
                if search_mode_val == 'hashtag':
                    search_query = f"#{clean_kw}" if clean_kw else keyword
                else:
                    search_query = f"{clean_kw} shop" if (clean_kw and 'shop' not in clean_kw.lower()) else clean_kw or keyword
                limit_capped = min(max(max_results, 5), 100)
                # Yêu cầu thêm kết quả (x3) để sau khi lọc reels/groups còn đủ posts
                fetch_count = min(limit_capped * 3, 100) if self.search_type == 'posts' else limit_capped
                mode_label = "hashtag" if search_mode_val == 'hashtag' else "keyword (shops)"

                # Fetch cả top (viral) và latest (mới nhất), merge + dedupe – ưu tiên viral + mới
                if self.search_type == 'posts':
                    fetch_per_run = max(fetch_count // 2, 5)
                    results_top = self.run_actor({
                        "query": search_query,
                        "resultsCount": fetch_per_run,
                        "searchType": "top",
                    })
                    results_latest = self.run_actor({
                        "query": search_query,
                        "resultsCount": fetch_per_run,
                        "searchType": "latest",
                    })
                    seen = set()
                    merged = []
                    for r in (results_top or []) + (results_latest or []):
                        if not isinstance(r, dict):
                            continue
                        pid = r.get('postId') or r.get('id', '')
                        if not pid and r.get('video'):
                            pid = r.get('video', {}).get('id', '')
                        if not pid and r.get('videoUrl'):
                            pid = r.get('videoUrl', '')
                        if pid and pid not in seen:
                            seen.add(pid)
                            merged.append(r)
                    results = merged
                    self.logger.info(f"Facebook Posts: merged top+latest -> {len(results)} unique (viral + newest)")
                else:
                    actor_input = {
                        "query": search_query,
                        "resultsCount": fetch_count,
                        "searchType": "latest",
                    }
                    results = self.run_actor(actor_input)
                self.logger.info(f"Using Facebook Posts Search: query='{search_query}' mode={mode_label} - filter reels/groups, return up to {limit_capped}")
                use_keyword_mode = False
            # Instagram: hashtag vs keyword (different actors for different results)
            elif self.platform == Platform.INSTAGRAM and not keyword.startswith('@') and not keyword.startswith('http'):
                import unicodedata
                search_mode_val = (search_mode or "hashtag").strip().lower()
                use_keyword_mode = search_mode_val == "keyword"

                # Pool size: Giới hạn 30-35 để tiết kiệm token (Variety vẫn hoạt động với buffer nhỏ)
                POOL_SIZE = min(max_results + 5, 35)
                results_type = "reels" if self.search_type == "reels" else "posts"
                
                self.actor_id = 'apify/instagram-hashtag-scraper'
                search_query = keyword.replace('#', '').strip()
                clean_keyword = search_query.replace(' ', '').strip()
                normalized = unicodedata.normalize('NFD', clean_keyword)
                clean_keyword = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
                
                if not clean_keyword:
                    clean_keyword = search_query.replace(' ', '')
                
                # Luôn dùng related terms để mở rộng pool (Variety logic)
                related_terms = self._get_related_terms(clean_keyword)[:3]
                # Sanitize lần cuối trước khi gửi actor (đảm bảo không có ký tự lạ)
                related_terms = [self._sanitize_hashtag(t) for t in related_terms]
                related_terms = [t for t in related_terms if t]  # Loại bỏ empty strings
                if not related_terms:
                    related_terms = [clean_keyword]
                limit_per_tag = max(POOL_SIZE // len(related_terms), 10)
                
                actor_input = {
                    "hashtags": related_terms,
                    "resultsType": results_type,
                    "resultsLimit": limit_per_tag,
                    "searchLimit": limit_per_tag,
                    "searchType": "hashtag",
                }
                self.logger.info(f"Using Instagram Variety-Hybrid for '{search_query}' - terms={related_terms}, limit={limit_per_tag}/tag")
            else:
                # Default input builder - truyền search_mode để TikTok phân biệt hashtag/keyword
                actor_input = self._build_actor_input(keyword, max_results, search_mode=search_mode)

            if results is None:
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
            
            # Facebook: dedupe (đã dedupe trong merge top+latest) và shuffle với seed theo thời gian → mỗi lần search thứ tự khác
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
                    rng = random.Random(int(time.time() * 1000))
                    rng.shuffle(results)
                    self.logger.info(f"Deduped to {len(results)} unique Facebook posts, shuffled (variety per search)")

            # Facebook Posts: loại bỏ reels/videos và bài từ hội nhóm – chỉ giữ posts từ shop/page
            if (self.platform == Platform.FACEBOOK and self.search_type == 'posts' and results):
                before = len(results)
                results = [r for r in results if isinstance(r, dict) and not self._is_facebook_reel_or_video(r)
                           and not self._is_facebook_group_post(r)]
                if before > len(results):
                    self.logger.info(f"Facebook posts filter: excluded {before - len(results)} reels/videos/group posts, kept {len(results)} posts")

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

            # Instagram: dedupe (có thể trùng khi dùng nhiều hashtag)
            # Sau đó random.sample từ pool để mỗi lần search ra kết quả khác nhau
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
                    # Dùng seed theo thời gian để mỗi lần chọn sample khác nhau
                    rng = random.Random(int(time.time() * 1000))
                    rng.shuffle(results)
                    self.logger.info(f"Deduped to {len(results)} unique posts, shuffled (time-seeded) for variety")
            
            # KEYWORD mode (hybrid): filter theo caption chứa keyword
            if self.platform == Platform.INSTAGRAM and results and use_keyword_mode:
                search_query = keyword.replace('#', '').strip()
                before_count = len(results)
                results_before_caption = list(results)
                results = [r for r in results if isinstance(r, dict) and self._caption_contains_keyword(r, search_query)]
                self.logger.info(f"Instagram caption filter: {before_count} -> {len(results)} posts (keyword='{search_query}')")
                if len(results) < before_count * 0.3 and before_count >= 10:
                    self.logger.info("Instagram caption filter quá chặt, giữ thêm bài không khớp caption để đủ kết quả")
                    results = self._sort_by_caption_relevance(results_before_caption, search_query)

            # Instagram: Engagement Quality Filter
            # Loại bỏ post có tương tác quá thấp (spam/rác) nếu có yêu cầu filter
            if self.platform == Platform.INSTAGRAM and (min_likes > 0 or min_views > 0 or min_comments > 0) and results:
                before_eng = len(results)
                filtered_ig = []
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    # Instagram dùng các field khác nhau tùy actor
                    likes = (r.get('likesCount') or r.get('likes') or 0)
                    views = (r.get('videoViewCount') or r.get('videoPlayCount') or r.get('views') or 0)
                    comments = (r.get('commentsCount') or r.get('comments') or 0)
                    try:
                        likes = int(likes) if likes else 0
                        views = int(views) if views else 0
                        comments = int(comments) if comments else 0
                    except (TypeError, ValueError):
                        likes = views = comments = 0
                    # OR logic: đạt BẤT KỲ 1 threshold → giữ lại
                    # Instagram ảnh thường không có views → dùng AND sẽ loại gần hết
                    # Chỉ loại bài KHÔNG đạt bất kỳ threshold nào (真正的 spam)
                    passes = (
                        (min_likes > 0 and likes >= min_likes) or
                        (min_views > 0 and views >= min_views) or
                        (min_comments > 0 and comments >= min_comments)
                    )
                    if passes:
                        filtered_ig.append(r)
                if filtered_ig:  # Chỉ áp dụng nếu còn kết quả
                    results = filtered_ig
                    self.logger.info(f"Instagram engagement filter: {before_eng} -> {len(results)} (min_likes={min_likes}, min_views={min_views})")
                else:
                    self.logger.info(f"Instagram engagement filter: bỏ qua (không còn kết quả nào thỏa mãn)")

            # Instagram: random.sample từ pool (không lấy từ đầu) để đa dạng hóa kết quả
            # Pool đã được shuffle (time-seeded) bên trên nên slice từ đầu = random sample thực sự
            if self.platform == Platform.INSTAGRAM and len(results) > max_results:
                results = results[:max_results]
                self.logger.info(f"Instagram: sampled {max_results} from pool (randomized each search)")

            # TikTok: Lọc theo độ liên quan (keyword mode) và chất lượng engagement.
            if self.platform == Platform.TIKTOK and results:
                search_mode_val = (search_mode or "hashtag").strip().lower()
                clean_kw = keyword.replace('#', '').strip()
                
                # --- BƯỚC 1: Keyword Relevance Filter ---
                # Khi search_mode='keyword': lọc video có description/hashtag chứa keyword
                # Khi search_mode='hashtag': TikTok đã đảm bảo hashtag match, không cần lọc thêm
                if search_mode_val == "keyword" and clean_kw:
                    before_kw = len(results)
                    results_before_kw = list(results)
                    relevant = [r for r in results if isinstance(r, dict) and self._tiktok_is_relevant(r, clean_kw)]
                    self.logger.info(f"TikTok keyword filter: {before_kw} -> {len(relevant)} (keyword='{clean_kw}')")
                    # Nếu filter quá chặt (<30%), giữ nguyên nhưng sort relevant lên trước
                    if len(relevant) < before_kw * 0.3 and before_kw >= 5:
                        self.logger.info("TikTok keyword filter quá chặt, sort by relevance thay vì bỏ")
                        results = self._tiktok_sort_by_relevance(results_before_kw, clean_kw)
                    else:
                        results = relevant
                
                # --- BƯỚC 2: Engagement Quality Filter ---
                # Loại bỏ video có tương tác quá thấp (spam/rác) nếu có yêu cầu filter
                if (min_likes > 0 or min_views > 0 or min_comments > 0) and results:
                    before_eng = len(results)
                    def _get_stat(r, keys, default=0):
                        for k in keys:
                            v = r.get(k) or (r.get('stats', {}) or r.get('statistics', {})).get(k, 0)
                            if v:
                                try: return int(v)
                                except: pass
                        return default
                    
                    filtered_by_eng = []
                    for r in results:
                        if not isinstance(r, dict):
                            continue
                        likes = _get_stat(r, ['likes', 'diggCount', 'heart'])
                        views = _get_stat(r, ['views', 'playCount', 'play_count'])
                        comments = _get_stat(r, ['comments', 'commentCount', 'comment_count'])
                        if likes >= min_likes and views >= min_views and comments >= min_comments:
                            filtered_by_eng.append(r)
                    
                    if filtered_by_eng:  # Chỉ áp dụng nếu còn kết quả
                        results = filtered_by_eng
                        self.logger.info(f"TikTok engagement filter: {before_eng} -> {len(results)} (min_likes={min_likes}, min_views={min_views})")
                    else:
                        self.logger.info(f"TikTok engagement filter: bỏ qua (không còn kết quả nào thỏa mãn)")
                
                # --- BƯỚC 3: Variety Expansion + Sort Trend + Random Sample ---
                # Nếu pool nhỏ, hãy thử fetch thêm profile/hashtag liên quan (đã build ở input)
                # Hoặc đơn giản là fetch dư ra 1.5x để sample.
                
                results = sorted(
                    results,
                    key=lambda r: self._compute_trend_score(r, 'tiktok'),
                    reverse=True  # cao nhất lên đầu
                )
                self.logger.info(f"TikTok: sorted by trend_score (viral + recency)")

                seed_val = int(time.time() * 1000) % (2**31)
                rng = random.Random(seed_val)

                # Variety: Sample từ pool lớn (nếu có)
                if len(results) >= max_results:
                    # Lấy pool rộng xíu (1.5x) để chọn 30 cái ngẫu nhiên
                    # Nếu len(results) == max_results, sample sẽ trả về đúng ngần đó nhưng order khác
                    # Để variety thực sự, pool_to_sample nên > max_results
                    pool_to_sample = results[:max(int(max_results * 1.5), len(results))]
                    results = rng.sample(pool_to_sample, min(max_results, len(pool_to_sample)))
                    self.logger.info(f"TikTok Variety: sampled {max_results} from {len(pool_to_sample)} (seed={seed_val})")
                else:
                    rng.shuffle(results)
                    self.logger.info(f"TikTok: shuffled {len(results)} (pool too small for variety)")

            # Instagram: Sort trend + random.sample từ pool lớn
            # Pool = 4× max_results (60 bài), sort viral+recent trước, sample ngẫu nhiên
            if self.platform == Platform.INSTAGRAM and results:
                # Sort trend trước: viral + gần đây → lên đầu pool
                results = sorted(
                    results,
                    key=lambda r: self._compute_trend_score(r, 'instagram'),
                    reverse=True
                )
                self.logger.info(f"Instagram: sorted by trend_score (viral + recency), pool={len(results)}")

                seed_val = int(time.time() * 1000) % (2**31)
                rng_ig = random.Random(seed_val)
                if len(results) > max_results:
                    # Sample trong top-half để đa dạng nhưng vẫn ưu tiên xu hướng
                    top_pool = results[:max(max_results * 2, len(results) // 2)]
                    results = rng_ig.sample(top_pool, min(max_results, len(top_pool)))
                    self.logger.info(f"Instagram: sampled {max_results} from top-{len(top_pool)} trend pool")
                else:
                    rng_ig.shuffle(results)
                    self.logger.info(f"Instagram: shuffled {len(results)} (pool too small)")
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
             
             # Clean user using robust normalizer
             clean_user = normalize_username(username)

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
        elif self.platform == Platform.XIAOHONGSHU:
            return self._normalize_xiaohongshu_data(raw_data)
        else:
            raise ScraperException(f"Unsupported platform: {self.platform}")

    def _normalize_xiaohongshu_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Xiaohongshu note data."""
        # Note ID
        note_id = raw_data.get('id', '') or raw_data.get('note_id', '')
        
        # Author
        user = raw_data.get('user', {})
        author_username = user.get('id', '') or user.get('user_id', 'unknown')
        author_name = user.get('nickname', '') or user.get('name', '') or author_username
        author_avatar = user.get('avatar', '') or user.get('image', '')
        
        # Stats
        likes = raw_data.get('likes', 0) or raw_data.get('liked_count', 0)
        collects = raw_data.get('collects', 0) or raw_data.get('collected_count', 0)
        comments = raw_data.get('comments', 0) or raw_data.get('comment_count', 0)
        shares = raw_data.get('shares', 0) or raw_data.get('share_count', 0)
        
        # Images/Video
        images_list = raw_data.get('images_list', []) or raw_data.get('images', [])
        image_list_urls = [img.get('url', img) if isinstance(img, dict) else img for img in images_list]
        
        cover_url = ''
        if image_list_urls:
            cover_url = image_list_urls[0]
        else:
            cover_url = raw_data.get('cover', {}).get('url', '')
            
        video_url = raw_data.get('video', {}).get('media', {}).get('stream', {}).get('h264', [{}])[0].get('master_url', '')

        # Metadata
        timestamp = raw_data.get('timestamp') or raw_data.get('create_time', 0)
        published_at = None
        if timestamp:
             try:
                published_at = self._parse_timestamp(timestamp)
             except:
                pass

        return {
            'video_id': str(note_id),
            'title': raw_data.get('title', ''),
            'description': raw_data.get('desc', '') or raw_data.get('description', ''),
            'author_username': author_username,
            'author_name': author_name,
            'likes_count': int(likes) if likes else 0,
            'views_count': 0, # XHS hides view count usually
            'comments_count': int(comments) if comments else 0,
            'shares_count': int(shares) if shares else 0,
            'video_url': f"https://www.xiaohongshu.com/explore/{note_id}",
            'download_url': video_url,
            'thumbnail_url': cover_url,
            'published_at': published_at,
            'hashtags': [], 
            'music_info': {},
            'raw_data': raw_data
        }
    
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
        
        # Stats (scraper_one: reactionsCount; hashtag: likesCount; video-search: reaction_count)
        likes = (data.get('reactionsCount') or data.get('likes') or data.get('likesCount') or
                 data.get('like_count') or data.get('reaction_count') or data.get('reactionCount') or 0)
        if isinstance(likes, dict):
            likes = likes.get('count', 0) or likes.get('totalCount', 0)
        comments = (data.get('comments') or data.get('commentsCount') or data.get('comment_count') or 0)
        if isinstance(comments, dict):
            comments = comments.get('count', 0)
        shares = (data.get('shares') or data.get('sharesCount') or data.get('share_count') or 0)
        if isinstance(shares, dict):
            shares = shares.get('count', 0)
        # Nested feedback (GraphQL-style)
        feedback = data.get('feedback', {}) or {}
        if isinstance(feedback, dict):
            if likes in (0, None, '') and feedback.get('reaction_count'):
                likes = feedback['reaction_count']
            if comments in (0, None, '') and feedback.get('comment_count'):
                comments = feedback['comment_count']
            if shares in (0, None, '') and feedback.get('share_count'):
                shares = feedback['share_count']
        
        # User Info (scraper_one: author; hashtag: user; video-search: video_owner_profile)
        user_data = data.get('user', {}) or data.get('author', {})
        author_username = ''
        author_name = ''
        
        if isinstance(user_data, dict):
            author_username = user_data.get('username') or user_data.get('uri_token', '') or user_data.get('id', '')
            author_name = user_data.get('name', '')
            # scraper_one: extract username from profileUrl (facebook.com/jeff.moerke.1)
            if not author_username and user_data.get('profileUrl'):
                try:
                    parts = str(user_data['profileUrl']).split('facebook.com/')[-1].split('/')
                    if parts:
                        author_username = parts[0]
                except Exception:
                    pass
        
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
        # scraper_one: attachments[{type:'video',url}]
        if not video_url and data.get('attachments'):
            for att in (data['attachments'] or []):
                if isinstance(att, dict) and att.get('type') == 'video' and att.get('url'):
                    video_url = _extract_url(att['url'])
                    if video_url:
                        break
        
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

        # 3a. scraper_one: attachments[{type:'photo'|'image'|'video',url,id,image,fullImage}]
        # NOTE: Do NOT use graph.facebook.com/{id}/picture - attachment IDs are NOT Graph API IDs.
        # scraper_one photo url thường là photo.php; ưu tiên image/fullImage nếu có (direct CDN).
        if not thumbnail_url and data.get('attachments'):
            for att in (data['attachments'] or []):
                if not isinstance(att, dict):
                    continue
                att_type = (att.get('type') or '').lower()
                # Photo: scraper_one có thể có image/fullImage (direct fbcdn)
                if att_type in ('photo', 'image'):
                    att_url = _extract_url(
                        att.get('image') or att.get('fullImage') or att.get('url') or
                        att.get('src') or att.get('link') or ''
                    )
                    if att_url and 'fbcdn.net' in att_url:
                        thumbnail_url = att_url
                        break
                    if att_url and att_url.startswith('http'):
                        thumbnail_url = att_url
                        break
                    media = att.get('media') or {}
                    if isinstance(media, dict):
                        img_obj = media.get('image') or {}
                        if isinstance(img_obj, dict):
                            uri = img_obj.get('uri') or img_obj.get('src') or img_obj.get('url') or ''
                            if uri:
                                thumbnail_url = uri
                                break
                # Video thumbnails
                if att_type == 'video':
                    att_url = _extract_url(att.get('thumbnail') or att.get('image') or att.get('src') or '')
                    if att_url and ('fbcdn.net' in att_url or att_url.startswith('http')):
                        thumbnail_url = att_url
                        break
                    media = att.get('media') or {}
                    if isinstance(media, dict):
                        img_obj = media.get('image') or media.get('thumbnail') or {}
                        if isinstance(img_obj, dict):
                            uri = img_obj.get('uri') or img_obj.get('src') or ''
                            if uri:
                                thumbnail_url = uri
                                break
        # 3b. hashtag-scraper: all_subattachments.nodes[].media.image.uri
        if not thumbnail_url and data.get('attachments') and isinstance(data.get('attachments'), list):
            for att in data['attachments']:
                nodes = (att.get('all_subattachments') or {}).get('nodes') if isinstance(att.get('all_subattachments'), dict) else []
                for node in (nodes or [])[:3]:
                    if not isinstance(node, dict):
                        continue
                    img = ((node.get('media') or {}).get('image') or {}) if isinstance(node.get('media'), dict) else {}
                    if isinstance(img, dict):
                        thumbnail_url = _extract_url(img.get('uri') or img.get('src') or img.get('url'))
                    if thumbnail_url:
                        break
                if thumbnail_url:
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
        # 6. Fallback: author profile picture (text-only posts)
        if not thumbnail_url and isinstance(user_data, dict) and user_data.get('profilePicture'):
            pic_url = _extract_url(user_data['profilePicture'])
            if pic_url and 'fbcdn.net' in pic_url:
                # Replace s40x40/s100x100 with s720x720 for larger thumbnail
                import re as _re2
                pic_url = _re2.sub(r'_s\d+x\d+', '_s720x720', pic_url)
            thumbnail_url = pic_url or ''
        
        # Construct Web URL (Permalink) - MUST BE BEFORE is_video detection (video-search uses videoUrl)
        permalink = _extract_url(data.get('url') or data.get('postUrl') or data.get('videoUrl'))
        if permalink and 'undefined' in permalink.lower():
            permalink = ''
        
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
            user_handle = (author_username or data.get('pageName') or '').strip()
            if not user_handle or user_handle.lower() in ('undefined', 'null', 'none'):
                if is_video:
                    permalink = f"https://www.facebook.com/watch/?v={post_id}"
                else:
                    permalink = f"https://www.facebook.com/permalink.php?story_fbid={post_id}"
            else:
                if is_video:
                    permalink = f"https://www.facebook.com/{user_handle}/videos/{post_id}/"
                else:
                    permalink = f"https://www.facebook.com/{user_handle}/posts/{post_id}/"

        # Content (scraper_one: postText; hashtag: text; posts-scraper: message)
        content = (data.get('postText') or data.get('text') or data.get('message') or data.get('title') or
                   data.get('description') or data.get('caption') or data.get('post_text') or data.get('content') or '')
        # Extract from nested video object (video-search-scraper)
        video_obj = data.get('video', {}) or {}
        if isinstance(video_obj, dict) and not content:
            content = video_obj.get('title') or video_obj.get('description') or ''
        # scraper_one: attachments[].accessibilityCaption trực tiếp trên attachment (không trong media)
        if not content and data.get('attachments'):
            for att in (data['attachments'] or []):
                if not isinstance(att, dict):
                    continue
                cap = att.get('accessibilityCaption') or att.get('accessibility_caption')
                if cap:
                    content = str(cap)
                    break
                media = att.get('media') or {}
                cap = (media.get('accessibility_caption') or media.get('caption') if isinstance(media, dict) else None)
                if cap:
                    content = str(cap)
                    break

        def _safe_int(val):
            if val is None:
                return 0
            if isinstance(val, int) and not isinstance(val, bool):
                return max(0, val)
            if isinstance(val, float):
                return max(0, int(val))
            if isinstance(val, str) and val.strip().replace('.', '').isdigit():
                return max(0, int(float(val)))
            return 0

        return {
            'video_id': str(post_id),
            'title': content or '',
            'description': content or '',
            'author_username': str(author_username or ''),
            'author_name': str(author_name or ''),
            'likes_count': _safe_int(likes),
            'views_count': _safe_int(data.get('views') or data.get('viewCount') or data.get('videoViewCount') or video_obj.get('view_count')),
            'comments_count': _safe_int(comments),
            'shares_count': _safe_int(shares),
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
            
            # If Unix timestamp (integer or float); scraper_one uses milliseconds
            if isinstance(timestamp, (int, float)):
                ts = float(timestamp)
                if ts > 1e12:  # milliseconds (e.g. 1743735597000)
                    ts = ts / 1000
                return datetime.fromtimestamp(int(ts), timezone.utc)
            
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
    +
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
