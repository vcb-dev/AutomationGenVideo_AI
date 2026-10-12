"""Apify Facebook scraper — fetch + parse only (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi scraper_fanpages / scraper_facebook_reels /
scraper_fanpage_metrics_history. AI chỉ gọi Apify + parse dữ liệu, trả dict
thô cho BE tự lưu.
"""

import html
import logging
import re
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as tz_dt
from typing import Optional, List, Dict, Any
from django.conf import settings

logger = logging.getLogger(__name__)

# Handle không phải tên page
_NON_PAGE_HANDLES = {'profile.php', 'watch', 'reel', 'reels', 'groups', 'share'}


def _get_apify_token() -> str:
    """Lấy API token của Apify từ settings hoặc env."""
    token = getattr(settings, 'APIFY_API_TOKEN', '') or ''
    return str(token).strip()


def clean_facebook_url(url: str) -> str:
    """Chuẩn hóa URL Facebook về dạng https://www.facebook.com/<handle>"""
    if not url:
        return ''
    cleaned = url.strip()
    cleaned = re.sub(r'[?&].*$', '', cleaned)
    cleaned = cleaned.rstrip('/')
    if not cleaned.startswith('http'):
        cleaned = 'https://' + cleaned.lstrip('/')
    if 'facebook.com' not in cleaned and 'fb.watch' not in cleaned:
        cleaned = f"https://www.facebook.com/{cleaned.lstrip('https://')}"
    return cleaned


def _extract_handle(url: str) -> str:
    """Trích handle từ URL fanpage. Trả '' nếu URL không chứa tên page."""
    m = re.search(r'facebook\.com/([^/?&#]+)', url or '')
    handle = m.group(1) if m else ''
    return '' if handle in _NON_PAGE_HANDLES else handle


def _to_int(value: Any, default: int = 0) -> int:
    """Ép kiểu số an toàn ('2.3M', '1,234', 1234, None)."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(',', '')
    if not text:
        return default
    multiplier = 1
    if text[-1].upper() in ('K', 'M', 'B'):
        multiplier = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}[text[-1].upper()]
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return default


def _extract_hashtags(text: str) -> List[str]:
    return [m.lstrip('#') for m in re.findall(r'#\w+', text or '')]


# ── Profile fetch ─────────────────────────────────────────────────────────────

def fetch_page_profile(page_url: str) -> Optional[dict]:
    """Gọi apify/facebook-pages-scraper lấy thông tin tổng quan Fanpage."""
    token = _get_apify_token()
    if not token:
        logger.warning("[APIFY-FB] APIFY_API_TOKEN chưa được cấu hình.")
        return None

    clean_url = clean_facebook_url(page_url)
    api_url = f"https://api.apify.com/v2/acts/apify~facebook-pages-scraper/run-sync-get-dataset-items?token={token}&timeout=120"
    payload = {
        "startUrls": [{"url": clean_url}]
    }

    try:
        resp = requests.post(api_url, json=payload, timeout=150)
        if resp.status_code in (200, 201):
            items = resp.json()
            if isinstance(items, list) and items:
                first = items[0]
                if first.get('error') or first.get('no_items'):
                    logger.info(f"[APIFY-PROFILE] {page_url}: Trả về rỗng ({first.get('errorDescription')})")
                    return None
                return first
        else:
            logger.warning(f"[APIFY-PROFILE] {page_url} thất bại: HTTP {resp.status_code} - {resp.text[:150]}")
            return None
    except Exception as e:
        logger.warning(f"[APIFY-PROFILE] Lỗi gọi API: {e}")
        return None


# ── Reels fetch ───────────────────────────────────────────────────────────────

def fetch_reels_only(
    page_url: str,
    num_of_posts: int = 50,
    exclude_post_ids: Optional[list] = None,
    start_date: str = '',
    profile: Optional[dict] = None,
) -> List[dict]:
    """Gọi apify/facebook-reels-scraper lấy danh sách Reels của Fanpage."""
    token = _get_apify_token()
    if not token:
        logger.warning("[APIFY-FB] APIFY_API_TOKEN chưa được cấu hình.")
        return []

    clean_url = clean_facebook_url(page_url)
    # Actor reels scraper hoạt động tốt nhất khi URL có hậu tố /reels/
    if not clean_url.endswith('/reels'):
        reels_target_url = f"{clean_url}/reels/"
    else:
        reels_target_url = f"{clean_url}/"

    exclude_set = set(str(pid) for pid in (exclude_post_ids or []))
    limit = max(1, min(int(num_of_posts or 50), 200))

    api_url = f"https://api.apify.com/v2/acts/apify~facebook-reels-scraper/run-sync-get-dataset-items?token={token}&timeout=180"
    payload: Dict[str, Any] = {
        "startUrls": [{"url": reels_target_url}],
        "resultsLimit": limit,
    }
    if start_date:
        payload["onlyPostsNewerThan"] = start_date

    logger.info(f"[APIFY-REELS] Bắt đầu cào Reels cho {reels_target_url} (limit={limit})")
    try:
        resp = requests.post(api_url, json=payload, timeout=210)
        if resp.status_code not in (200, 201):
            logger.error(f"[APIFY-REELS] Apify từ chối (HTTP {resp.status_code}): {resp.text[:180]}")
            return []

        raw_items = resp.json()
        if not isinstance(raw_items, list):
            logger.warning(f"[APIFY-REELS] Response không phải mảng: {raw_items}")
            return []

        valid_reels = []
        for item in raw_items:
            # Kiểm tra lỗi đặc thù của Apify khi page không có reel hoặc rỗng
            if item.get('error') or item.get('no_items'):
                logger.info(f"[APIFY-REELS] {page_url}: {item.get('errorDescription') or 'No items'}")
                continue

            post_id = str(
                item.get('video', {}).get('id')
                or item.get('tracking', {}).get('top_level_post_id')
                or item.get('playback_video', {}).get('id')
                or ''
            )
            if post_id and post_id in exclude_set:
                continue

            valid_reels.append(item)

        logger.info(f"[APIFY-REELS] {page_url}: Đã lấy được {len(valid_reels)} reels hợp lệ.")
        return valid_reels

    except Exception as e:
        logger.error(f"[APIFY-REELS] Lỗi gọi Apify reels scraper: {e}")
        return []


# ── Parser helpers ────────────────────────────────────────────────────────────

def parse_fanpage_profile(profile: Optional[dict], first_reel: dict) -> Optional[dict]:
    """Parse profile từ Facebook Pages Scraper hoặc từ video_owner trong reel đầu tiên."""
    author = (first_reel or {}).get('video_owner') or (first_reel or {}).get('author') or {}
    delegate = author.get('delegate_page') or {}

    profile_id = str(
        (profile or {}).get('pageId')
        or (profile or {}).get('facebookId')
        or delegate.get('id')
        or author.get('id')
        or ''
    ).strip()

    if not profile_id:
        return None

    # Tên page
    name = (
        (profile or {}).get('title')
        or (profile or {}).get('pageName')
        or author.get('name')
        or ''
    )
    name = re.sub(r'\s*\|.*$', '', name).strip()

    # URL page
    page_url = (
        (profile or {}).get('pageUrl')
        or (profile or {}).get('facebookUrl')
        or author.get('url')
        or ''
    )

    # Avatar URL
    avatar_url = (
        (profile or {}).get('profilePictureUrl')
        or (profile or {}).get('profilePic')
        or (author.get('displayPicture') or {}).get('uri')
        or ''
    )

    # Verified
    is_verified = (profile or {}).get('verified') or author.get('is_verified')
    if is_verified is not None:
        is_verified = bool(is_verified)

    # Followers
    followers_count = _to_int((profile or {}).get('followers'), default=0)

    return {
        'profile_id': profile_id,
        'name': name,
        'page_url': page_url,
        'handle': _extract_handle(page_url),
        'avatar_url': avatar_url,
        'is_verified': is_verified,
        'followers_count': followers_count,
    }


def parse_facebook_reels(reels_list: list) -> List[dict]:
    """Parse reels dataset từ Apify thành danh sách dict chuẩn cho BE."""
    parsed = []

    for reel in reels_list:
        post_id = str(
            reel.get('video', {}).get('id')
            or reel.get('tracking', {}).get('top_level_post_id')
            or reel.get('playback_video', {}).get('id')
            or reel.get('post_id')
            or ''
        )
        if not post_id:
            continue

        shortcode = post_id

        # Reel URL
        url = (
            reel.get('shareable_url')
            or reel.get('topLevelReelUrl')
            or reel.get('url')
            or f"https://www.facebook.com/reel/{post_id}/"
        )

        # Content / Caption
        content = reel.get('text') or reel.get('caption') or reel.get('description') or ''

        # Video URL (MP4 trực tiếp)
        pv = reel.get('playback_video') or {}
        vdl = pv.get('videoDeliveryLegacyFields') or {}
        video_url = (
            vdl.get('browser_native_hd_url')
            or vdl.get('browser_native_sd_url')
            or reel.get('browser_native_hd_url')
            or reel.get('browser_native_sd_url')
            or pv.get('permalink_url')
            or ''
        )

        # Thumbnail
        thumb_image = pv.get('thumbnailImage') or {}
        thumbnail_url = (
            thumb_image.get('uri')
            or reel.get('video', {}).get('first_frame_thumbnail')
            or reel.get('thumbnail_uri')
            or ''
        )

        # Duration
        duration_seconds = (
            pv.get('length_in_second')
            or (reel.get('video', {}).get('playable_duration_in_ms', 0) / 1000.0 if reel.get('video', {}).get('playable_duration_in_ms') else None)
        )
        if duration_seconds:
            duration_seconds = round(float(duration_seconds), 1)

        # Date posted
        time_str = reel.get('time') or reel.get('date_posted')
        if time_str:
            try:
                date_posted = time_str
            except Exception:
                date_posted = datetime.now(tz=tz_dt.utc).isoformat()
        else:
            date_posted = datetime.now(tz=tz_dt.utc).isoformat()

        # Views count
        views_count = _to_int(
            reel.get('playCountRounded')
            or reel.get('play_count_reduced')
            or reel.get('play_count')
            or 0
        )

        # Likes / Comments / Shares
        likes_count = _to_int(reel.get('likes_count') or reel.get('reactions_count') or 0)
        comments_count = _to_int(reel.get('comments_count') or 0)
        shares_count = _to_int(reel.get('shares_count') or reel.get('reshare_count') or 0)

        parsed.append({
            'post_id': post_id,
            'shortcode': shortcode,
            'url': url,
            'content': content,
            'hashtags': _extract_hashtags(content),
            'video_url': video_url,
            'thumbnail_url': thumbnail_url,
            'duration_seconds': duration_seconds,
            'has_audio': True,
            'date_posted': date_posted,
            'views_count': views_count,
            'likes_count': likes_count,
            'comments_count': comments_count,
            'shares_count': shares_count,
        })

    # Bổ sung caption từ OpenGraph nếu Apify trả về content rỗng
    empty_indices = [i for i, r in enumerate(parsed) if not r.get('content') and r.get('post_id')]
    if empty_indices:
        def _enrich(idx: int):
            pid = parsed[idx]['post_id']
            cap = _fetch_og_caption(pid)
            if cap:
                parsed[idx]['content'] = cap
                parsed[idx]['hashtags'] = _extract_hashtags(cap)

        with ThreadPoolExecutor(max_workers=min(10, len(empty_indices))) as ex:
            list(ex.map(_enrich, empty_indices))

    return parsed


_FB_BOT_HEADERS = {
    'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)',
    'Accept-Language': 'vi,en;q=0.9',
}


def _fetch_og_caption(post_id_or_url: str) -> str:
    """Trích xuất caption từ OpenGraph metadata của Facebook Reel."""
    if not post_id_or_url:
        return ''
    url = post_id_or_url if post_id_or_url.startswith('http') else f"https://www.facebook.com/reel/{post_id_or_url}/"
    try:
        resp = requests.get(url, headers=_FB_BOT_HEADERS, timeout=4)
        if resp.status_code == 200:
            # 1. Thử lấy og:description
            m = re.search(r'<meta property="og:description" content="([^"]+)"', resp.text)
            if m:
                text = html.unescape(m.group(1)).strip()
                if text and not text.lower().startswith('watch the latest reel from'):
                    return text
            # 2. Thử lấy og:title (thường có dạng "Nội dung caption | Tên Fanpage")
            m = re.search(r'<meta property="og:title" content="([^"]+)"', resp.text)
            if m:
                text = html.unescape(m.group(1)).strip()
                text = re.sub(r'\s*\|\s*[^|]+$', '', text).strip()
                if text:
                    return text
    except Exception:
        pass
    return ''
