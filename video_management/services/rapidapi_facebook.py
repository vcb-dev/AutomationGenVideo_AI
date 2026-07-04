"""RapidAPI Facebook scraper cho scraper_fanpages + scraper_facebook_reels."""

import logging
import re
import time
import requests
from datetime import datetime, timezone as tz_dt
from typing import Optional
from urllib.parse import urlparse
from django.conf import settings
from django.utils import timezone

from ..models_scraper import ScrapedFanpage, FacebookReel, FanpageMetricsHistory

logger = logging.getLogger(__name__)


def clean_facebook_url(raw_url: str) -> str:
    """Normalize a Facebook URL to canonical form.

    Examples:
        https://www.facebook.com/katjewelry/?locale=vi_VN  -> https://www.facebook.com/katjewelry/
        https://www.facebook.com/profile.php?id=100064880610399  -> kept as-is (profile ID)
    """
    if not raw_url:
        return ''
    parsed = urlparse(raw_url)
    path = parsed.path.rstrip('/') + '/'

    # profile.php?id=xxx → keep query
    if 'profile.php' in path:
        pid = dict(p.split('=') for p in parsed.query.split('&') if '=' in p).get('id', '')
        if pid:
            return f"https://www.facebook.com/profile.php?id={pid}"

    return f"https://www.facebook.com{path}"


def extract_handle_from_url(url: str) -> str:
    """Extract the page handle from a clean Facebook URL.

    https://www.facebook.com/katjewelry/  -> katjewelry
    https://www.facebook.com/profile.php?id=100064880610399  -> ''
    """
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    if not path or path == 'profile.php':
        return ''
    # Nếu path chứa / (sub-path) thì không phải handle đơn giản
    if '/' in path:
        return ''
    return path


def _rapidapi_host() -> str:
    return getattr(settings, 'RAPIDAPI_FACEBOOK_HOST', 'facebook-scraper-api4.p.rapidapi.com')


def _rapidapi_profile_url() -> str:
    return f"https://{_rapidapi_host()}/get_facebook_pages_details_from_link"


def _rapidapi_reels_url() -> str:
    return f"https://{_rapidapi_host()}/get_facebook_reels_details"


def _headers() -> dict:
    api_key = getattr(settings, 'RAPIDAPI_FACEBOOK_KEY', '')
    if not api_key:
        raise ValueError("RAPIDAPI_FACEBOOK_KEY not configured")
    return {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": _rapidapi_host(),
        "Content-Type": "application/json",
    }


# ── Profile detail ────────────────────────────────────────────────────────────

def fetch_page_profile(page_url: str) -> Optional[dict]:
    """Gọi profile detail API, trả về dict page hoặc None nếu lỗi."""
    try:
        resp = requests.get(
            _rapidapi_profile_url(),
            headers=_headers(),
            params={
                "link": page_url,
                "exact_followers_count": "true",
                "show_verified_badge": "false",
                "proxy_country": "us",
                "page_section": "default",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception as e:
        logger.warning(f"[FB-PROFILE] {page_url}: {e}")
        return None


# ── Reels fetch (có pagination) ───────────────────────────────────────────────

def _fetch_reels_page(page_url: str, cursor: Optional[str] = None) -> tuple:
    """Fetch 1 trang reels. Returns (reels_list, next_cursor, has_next)."""
    params = {"link": page_url}
    if cursor:
        params["cursor"] = cursor
    try:
        resp = requests.get(
            _rapidapi_reels_url(),
            headers=_headers(),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        inner = resp.json().get("data", {})
        reels = inner.get("reels", [])
        page_info = inner.get("page_info", {})
        has_next = bool(page_info.get("has_next"))
        next_cursor = page_info.get("end_cursor") if has_next else None
        return reels, next_cursor, has_next
    except Exception as e:
        logger.warning(f"[FB-REELS] {page_url}: {e}")
        return [], None, False


# ── Public interface ──────────────────────────────────────────────────────────

def fetch_reels_only(
    page_url: str,
    num_of_posts: int = 30,
    exclude_post_ids: Optional[list] = None,
    start_date: str = '',
) -> list:
    """Chỉ fetch reels, không gọi profile API. Returns reels_list."""
    exclude_set = set(str(x) for x in (exclude_post_ids or []))
    start_dt: Optional[datetime] = None
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=tz_dt.utc)
        except ValueError:
            pass

    all_reels: list = []
    cursor: Optional[str] = None

    while len(all_reels) < num_of_posts:
        batch, next_cursor, has_next = _fetch_reels_page(page_url, cursor)
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


def scrape_reels_sync(
    page_url: str,
    num_of_posts: int = 30,
    exclude_post_ids: Optional[list] = None,
    start_date: str = '',
    end_date: str = '',
) -> tuple:
    """Gọi cả profile API lẫn reels API. Returns (profile_dict, reels_list).

    - profile_dict: thông tin page (None nếu lỗi)
    - reels_list: list reel dicts đã lọc dedup + start_date
    """
    profile = fetch_page_profile(page_url)

    exclude_set = set(str(x) for x in (exclude_post_ids or []))
    start_dt: Optional[datetime] = None
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=tz_dt.utc)
        except ValueError:
            pass

    all_reels: list = []
    cursor: Optional[str] = None

    while len(all_reels) < num_of_posts:
        batch, next_cursor, has_next = _fetch_reels_page(page_url, cursor)
        if not batch:
            break

        reached_old = False
        for reel in batch:
            post_id = str(reel.get('post_id', '') or '')
            if not post_id:
                continue
            if post_id in exclude_set:
                continue
            if start_dt:
                ts = reel.get('timestamp') or 0
                reel_dt = datetime.fromtimestamp(ts, tz=tz_dt.utc) if ts else None
                # API trả mới → cũ; khi gặp reel cũ hơn start_date thì dừng hẳn
                if reel_dt and reel_dt < start_dt:
                    reached_old = True
                    break
            all_reels.append(reel)
            if len(all_reels) >= num_of_posts:
                break

        if reached_old or not has_next or not next_cursor:
            break
        cursor = next_cursor
        time.sleep(0.5)  # tránh rate-limit giữa các trang

    logger.info(f"[FB-REELS] {page_url}: profile={'OK' if profile else 'N/A'}, reels={len(all_reels)}")
    return profile, all_reels


# ── Helper: parse ─────────────────────────────────────────────────────────────

def _extract_handle(url: str) -> str:
    m = re.search(r'facebook\.com/([^/?&#]+)', url or '')
    return m.group(1) if m else ''


def _extract_hashtags(text: str) -> list:
    return [m.lstrip('#') for m in re.findall(r'#\w+', text or '')]


def _upsert_fanpage(
    profile: Optional[dict],
    first_reel: dict,
    fanpage: Optional[ScrapedFanpage],
) -> Optional[ScrapedFanpage]:
    """Upsert ScrapedFanpage từ profile data. Fallback sang author trong reel nếu profile=None."""
    author = first_reel.get('author') or {}
    delegate = author.get('delegate_page') or {}

    profile_id = str(
        (profile or {}).get('ad_page_id', '')
        or delegate.get('id', '')
        or author.get('id', '')
        or ''
    ).strip()
    if not profile_id:
        return None

    existing = ScrapedFanpage.objects.filter(profile_id=profile_id).first()
    if existing:
        if fanpage and fanpage.id != existing.id:
            fanpage.delete()
        fanpage = existing
    elif fanpage:
        # Placeholder có profile_id='' hoặc dạng 'tmp_...' → ghi đè bằng profile_id thật
        if not fanpage.profile_id or fanpage.profile_id.startswith('tmp_'):
            fanpage.profile_id = profile_id
            fanpage.save(update_fields=['profile_id'])
    else:
        fanpage, _ = ScrapedFanpage.objects.get_or_create(
            profile_id=profile_id,
            defaults={'name': profile_id},
        )

    # Giá trị ưu tiên: profile API > author trong reel > giữ nguyên
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
        followers_count = int(fanpage.followers_count or 0)

    if name:
        fanpage.name = name
    if page_url:
        fanpage.page_url = page_url
    handle = _extract_handle(page_url)
    if handle:
        fanpage.handle = handle

    prev_avatar = fanpage.avatar_url
    if avatar_url:
        fanpage.avatar_url = avatar_url

    is_verified = author.get('is_verified')
    if is_verified is not None:
        fanpage.is_verified = is_verified

    if followers_count > 0:
        fanpage.followers_count = followers_count

    fanpage.is_visible_on_ui = True
    fanpage.save()

    # Upload ảnh đại diện lên Drive nếu URL thay đổi hoặc chưa có drive URL
    from ..tasks import upload_thumbnail_to_drive_task
    if fanpage.avatar_url and (fanpage.avatar_url != prev_avatar or not fanpage.avatar_drive_url):
        upload_thumbnail_to_drive_task.delay(
            model='facebook_scraped_avatar',
            object_id=fanpage.id,
            cdn_url=fanpage.avatar_url,
            filename=f'fb-scraped-avatar-{fanpage.id}.jpg',
        )

    return fanpage


# ── Ingest ────────────────────────────────────────────────────────────────────

def ingest_reels_data(
    reels_list: list,
    fanpage: Optional[ScrapedFanpage] = None,
    profile: Optional[dict] = None,
) -> dict:
    """Parse reels JSON (RapidAPI format) và lưu vào DB.

    Returns: {'fanpage_id', 'created', 'updated', 'skipped'}
    """
    # Upsert fanpage từ profile data (nếu có) ngay cả khi reels trống
    first_reel = reels_list[0] if reels_list else {}
    if profile or reels_list:
        fanpage = _upsert_fanpage(profile, first_reel, fanpage)
    if not fanpage:
        return {'fanpage_id': None, 'created': 0, 'updated': 0, 'skipped': 0}

    if not reels_list:
        fanpage.last_scraped_at = timezone.now()
        fanpage.scraping_status = 'completed'
        fanpage.scrape_error = None
        fanpage.save(update_fields=['last_scraped_at', 'scraping_status', 'scrape_error', 'updated_at'])
        return {'fanpage_id': fanpage.id, 'created': 0, 'updated': 0, 'skipped': 0}

    FanpageMetricsHistory.objects.create(
        fanpage=fanpage,
        followers_count=fanpage.followers_count,
        likes_count=fanpage.likes_count,
    )

    created = updated = skipped = 0

    for reel in reels_list:
        post_id = str(reel.get('post_id') or '')
        # video_id dùng làm shortcode (unique per reel)
        shortcode = str(reel.get('video_id') or post_id)
        if not post_id or not shortcode:
            skipped += 1
            continue

        ts = reel.get('timestamp') or 0
        date_posted = (
            datetime.fromtimestamp(ts, tz=tz_dt.utc) if ts else timezone.now()
        )

        description = reel.get('description') or ''
        hashtags = _extract_hashtags(description)

        video_files = reel.get('video_files') or {}
        video_url = video_files.get('video_hd_file') or video_files.get('video_sd_file') or ''
        thumbnail_url = reel.get('thumbnail_uri') or ''

        defaults = {
            'fanpage': fanpage,
            'shortcode': shortcode,
            'url': reel.get('url') or '',
            'content': description,
            'hashtags': hashtags,
            'video_url': video_url,
            'thumbnail_url': thumbnail_url,
            'duration_seconds': reel.get('length_in_second'),
            'has_audio': True,  # API không trả về, mặc định True
            'date_posted': date_posted,
            'views_count': int(reel.get('play_count') or 0),
            'likes_count': int(reel.get('reactions_count') or 0),
            'comments_count': int(reel.get('comments_count') or 0),
            'shares_count': int(reel.get('reshare_count') or 0),
        }

        obj, was_created = FacebookReel.objects.update_or_create(
            post_id=post_id,
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1

        # Upload thumbnail lên Drive (ảnh có TTL, cần lưu lại)
        if thumbnail_url and (was_created or not obj.thumbnail_drive_url):
            from ..tasks import upload_thumbnail_to_drive_task
            upload_thumbnail_to_drive_task.delay(
                model='facebook_reel',
                object_id=obj.id,
                cdn_url=thumbnail_url,
                filename=f'fb-reel-{post_id}.jpg',
            )

    fanpage.last_scraped_at = timezone.now()
    fanpage.scraping_status = 'completed'
    fanpage.scrape_error = None
    if not fanpage.is_initial_scraped and created > 0:
        fanpage.is_initial_scraped = True
    fanpage.save(update_fields=['last_scraped_at', 'scraping_status', 'scrape_error', 'is_initial_scraped', 'updated_at'])

    logger.info(f"[FB-REELS] {fanpage.name}: +{created} mới, ~{updated} cập nhật, {skipped} bỏ qua")
    return {'fanpage_id': fanpage.id, 'created': created, 'updated': updated, 'skipped': skipped}


def save_profile_to_db(profile: dict, fanpage: ScrapedFanpage) -> Optional[ScrapedFanpage]:
    """Lưu profile data vào DB ngay lập tức mà không cần chờ reels.

    Dùng để FE có thể hiển thị thông tin page sớm trong khi reels vẫn đang được cào.
    """
    return _upsert_fanpage(profile, {}, fanpage)


def discover_and_verify_page(page_url: str, fanpage: Optional[ScrapedFanpage] = None) -> Optional[ScrapedFanpage]:
    """Verify page hợp lệ bằng cách cào 1 reel thử. Returns ScrapedFanpage nếu OK."""
    try:
        profile, reels = scrape_reels_sync(page_url, num_of_posts=1)
    except Exception as e:
        logger.warning(f"[VERIFY] {page_url}: {e}")
        return None

    if not reels:
        logger.info(f"[VERIFY] Không có reels cho {page_url}")
        return None

    result = ingest_reels_data(reels, fanpage=fanpage, profile=profile)
    if result['fanpage_id']:
        return ScrapedFanpage.objects.get(id=result['fanpage_id'])
    return None
