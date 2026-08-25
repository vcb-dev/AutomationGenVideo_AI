"""RapidAPI Facebook scraper — fetch + parse only (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi scraper_fanpages/scraper_facebook_reels/
scraper_fanpage_metrics_history. AI chỉ gọi RapidAPI + parse dữ liệu, trả dict
thô cho BE tự lưu.
"""

import logging
import re
import time
import requests
from datetime import datetime, timezone as tz_dt
from typing import Optional
from django.conf import settings

logger = logging.getLogger(__name__)


def _rapidapi_host() -> str:
    return getattr(settings, 'RAPIDAPI_FACEBOOK_HOST', 'facebook-scraper-api4.p.rapidapi.com')


def _rapidapi_profile_url() -> str:
    return f"https://{_rapidapi_host()}/get_facebook_pages_details_from_link"


def _rapidapi_reels_url() -> str:
    return f"https://{_rapidapi_host()}/get_facebook_reels_details"


def _get_api_keys() -> list:
    keys = []
    primary = getattr(settings, 'RAPIDAPI_FACEBOOK_KEY', '') or ''
    backup = getattr(settings, 'RAPIDAPI_FACEBOOK_KEY_BACKUP', '') or ''
    for raw in [primary, backup]:
        for k in raw.split(','):
            cleaned = k.strip()
            if cleaned and cleaned not in keys:
                keys.append(cleaned)
    return keys


def _request_with_keys(url: str, params: dict, timeout: int = 30) -> Optional[requests.Response]:
    """Thực hiện HTTP GET với cơ chế tự động xoay vòng key khi gặp 429 hoặc hết hạn mức."""
    keys = _get_api_keys()
    if not keys:
        logger.warning("[FB-RAPIDAPI] RAPIDAPI_FACEBOOK_KEY chưa được cấu hình.")
        return None

    host = _rapidapi_host()
    for idx, key in enumerate(keys):
        headers = {
            "x-rapidapi-key": key,
            "x-rapidapi-host": host,
            "Content-Type": "application/json",
        }
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 429 or "exceeded the MONTHLY quota" in resp.text:
                logger.warning(f"[FB-RAPIDAPI] Key #{idx + 1} ({key[:8]}...) hết hạn mức (429). Đang chuyển sang key tiếp theo...")
                continue
            if resp.status_code == 403 and "not subscribed" in resp.text:
                logger.warning(f"[FB-RAPIDAPI] Key #{idx + 1} ({key[:8]}...) chưa subscribe API. Đang chuyển sang key tiếp theo...")
                continue
            resp.raise_for_status()
            return resp
        except Exception as e:
            if idx < len(keys) - 1:
                logger.warning(f"[FB-RAPIDAPI] Key #{idx + 1} gặp lỗi ({e}). Đang thử key tiếp theo...")
            else:
                logger.error(f"[FB-RAPIDAPI] Tất cả các RapidAPI key đều thất bại: {e}")
    return None


def _is_scraper3(host: str) -> bool:
    return 'facebook-scraper3' in host


# ── Profile detail ────────────────────────────────────────────────────────────

def fetch_page_profile(page_url: str) -> Optional[dict]:
    """Gọi profile detail API, trả về dict page hoặc None nếu lỗi."""
    host = _rapidapi_host()
    if _is_scraper3(host):
        url = f"https://{host}/page/details"
        params = {"url": page_url}
        resp = _request_with_keys(url, params=params, timeout=30)
        if not resp:
            return None
        try:
            data = resp.json()
            res = data.get("results")
            if res and isinstance(res, dict):
                return {
                    "title": res.get("name"),
                    "ad_page_id": str(res.get("page_id") or ""),
                    "url": res.get("url") or page_url,
                    "image": res.get("image") or "",
                    "followers_count": res.get("followers") or 0,
                    "reels_page_id": res.get("reels_page_id") or "",
                }
            return None
        except Exception as e:
            logger.warning(f"[FB-PROFILE-SCRAPER3] {page_url}: {e}")
            return None

    url = f"https://{host}/get_facebook_pages_details_from_link"
    params = {
        "link": page_url,
        "exact_followers_count": "true",
        "show_verified_badge": "false",
        "proxy_country": "us",
        "page_section": "default",
    }
    resp = _request_with_keys(url, params=params, timeout=30)
    if not resp:
        return None

    try:
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception as e:
        logger.warning(f"[FB-PROFILE-API4] {page_url}: {e}")
        return None


# ── Reels fetch (có pagination) ───────────────────────────────────────────────

def _fetch_reels_page(page_url: str, cursor: Optional[str] = None, reels_page_id: Optional[str] = None) -> tuple:
    """Fetch 1 trang reels. Returns (reels_list, next_cursor, has_next)."""
    host = _rapidapi_host()
    if _is_scraper3(host):
        url = f"https://{host}/page/reels"
        params = {}
        if reels_page_id:
            params["reels_page_id"] = reels_page_id
        else:
            prof = fetch_page_profile(page_url)
            r_id = (prof or {}).get("reels_page_id")
            if r_id:
                params["reels_page_id"] = r_id
            else:
                params["url"] = page_url
        if cursor:
            params["cursor"] = cursor

        resp = _request_with_keys(url, params=params, timeout=30)
        if not resp:
            return [], None, False
        try:
            data = resp.json()
            reels = data.get("results", [])
            next_cursor = data.get("cursor")
            has_next = bool(next_cursor)
            return reels, next_cursor, has_next
        except Exception as e:
            logger.warning(f"[FB-REELS-SCRAPER3] {page_url}: {e}")
            return [], None, False

    url = f"https://{host}/get_facebook_reels_details"
    params = {"link": page_url}
    if cursor:
        params["cursor"] = cursor

    resp = _request_with_keys(url, params=params, timeout=30)
    if not resp:
        return [], None, False

    try:
        inner = resp.json().get("data", {})
        reels = inner.get("reels", [])
        page_info = inner.get("page_info", {})
        has_next = bool(page_info.get("has_next"))
        next_cursor = page_info.get("end_cursor") if has_next else None
        return reels, next_cursor, has_next
    except Exception as e:
        logger.warning(f"[FB-REELS-API4] {page_url}: {e}")
        return [], None, False


def fetch_reels_only(
    page_url: str,
    num_of_posts: int = 30,
    exclude_post_ids: Optional[list] = None,
    start_date: str = '',
) -> list:
    """Chỉ fetch reels, không gọi profile API. Returns reels_list (raw, chưa parse)."""
    exclude_set = set(str(x) for x in (exclude_post_ids or []))
    start_dt: Optional[datetime] = None
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=tz_dt.utc)
        except ValueError:
            pass

    all_reels: list = []
    cursor: Optional[str] = None
    reels_page_id: Optional[str] = None

    if _is_scraper3(_rapidapi_host()):
        prof = fetch_page_profile(page_url)
        reels_page_id = (prof or {}).get("reels_page_id")

    while len(all_reels) < num_of_posts:
        batch, next_cursor, has_next = _fetch_reels_page(page_url, cursor, reels_page_id)
        if not batch:
            break

        reached_old = False
        for reel in batch:
            post_id = str(reel.get('post_id', '') or '')
            if not post_id or post_id in exclude_set:
                continue
            if start_dt:
                ts = reel.get('timestamp') or 0
                reel_dt = datetime.fromtimestamp(ts, tz=tz_dt.utc) if ts else None
                if reel_dt and reel_dt < start_dt:
                    reached_old = True
                    break
            all_reels.append(reel)
            if len(all_reels) >= num_of_posts:
                break

        if reached_old or not has_next or not next_cursor:
            break
        cursor = next_cursor
        time.sleep(0.5)

    return all_reels


# ── Helper: parse ─────────────────────────────────────────────────────────────

def _extract_handle(url: str) -> str:
    m = re.search(r'facebook\.com/([^/?&#]+)', url or '')
    return m.group(1) if m else ''


def _extract_hashtags(text: str) -> list:
    return [m.lstrip('#') for m in re.findall(r'#\w+', text or '')]


def parse_fanpage_profile(profile: Optional[dict], first_reel: dict) -> Optional[dict]:
    """Parse profile API data + author trong reel đầu tiên thành dict thô (không ghi DB).

    Giá trị ưu tiên: profile API > author trong reel. BE tự quyết định field nào
    ghi đè (chỉ ghi khi có giá trị — khớp hành vi _upsert_fanpage cũ).
    """
    author = (first_reel or {}).get('author') or {}
    delegate = author.get('delegate_page') or {}

    profile_id = str(
        (profile or {}).get('ad_page_id', '')
        or delegate.get('id', '')
        or author.get('id', '')
        or ''
    ).strip()
    if not profile_id:
        return None

    if profile:
        raw_name = profile.get('title') or author.get('name') or ''
        name = re.sub(r'\s*\|.*$', '', raw_name).strip()
        page_url = profile.get('url') or author.get('url') or ''
        avatar_url = profile.get('image') or (author.get('displayPicture') or {}).get('uri') or ''
        followers_count = int(profile.get('followers_count') or 0)
    else:
        name = author.get('name') or ''
        page_url = author.get('url') or ''
        avatar_url = (author.get('displayPicture') or {}).get('uri') or ''
        followers_count = 0

    return {
        'profile_id': profile_id,
        'name': name,
        'page_url': page_url,
        'handle': _extract_handle(page_url),
        'avatar_url': avatar_url,
        'is_verified': author.get('is_verified'),
        'followers_count': followers_count,
    }


def parse_facebook_reels(reels_list: list) -> list:
    """Parse reels JSON (RapidAPI format) thành list dict thô (không ghi DB)."""
    parsed = []

    for reel in reels_list:
        post_id = str(reel.get('post_id') or '')
        # video_id dùng làm shortcode (unique per reel)
        shortcode = str(reel.get('video_id') or post_id)
        if not post_id or not shortcode:
            continue

        ts = reel.get('timestamp') or 0
        date_posted = (
            datetime.fromtimestamp(ts, tz=tz_dt.utc) if ts else datetime.now(tz=tz_dt.utc)
        )

        description = reel.get('description') or ''
        video_files = reel.get('video_files') or {}

        parsed.append({
            'post_id': post_id,
            'shortcode': shortcode,
            'url': reel.get('url') or '',
            'content': description,
            'hashtags': _extract_hashtags(description),
            'video_url': video_files.get('video_hd_file') or video_files.get('video_sd_file') or '',
            'thumbnail_url': reel.get('thumbnail_uri') or '',
            'duration_seconds': reel.get('length_in_second'),
            'has_audio': True,  # API không trả về, mặc định True
            'date_posted': date_posted.isoformat(),
            'views_count': int(reel.get('play_count') or 0),
            'likes_count': int(reel.get('reactions_count') or 0),
            'comments_count': int(reel.get('comments_count') or 0),
            'shares_count': int(reel.get('reshare_count') or 0),
        })

    return parsed
