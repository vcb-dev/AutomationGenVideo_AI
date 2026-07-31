"""TikHub Instagram Reels scraper — fetch-only (không đụng DB).

Flow: User nhập username → TikHub cào reels (paginated) → parse thành dict.
BE (Prisma) là nơi upsert vào scraper_instagram_profiles/scraper_instagram_reels/
scraper_instagram_profile_metrics.
"""

import logging
import re
import requests
from datetime import datetime
from typing import Optional
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _tikhub_base() -> str:
    return getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')


def fetch_user_info(username: str) -> Optional[dict]:
    """Fetch Instagram user info qua TikHub (follower counts, bio, v.v.).

    Returns raw `data` dict từ TikHub, hoặc None nếu lỗi.
    """
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")

    resp = requests.get(
        f'{_tikhub_base()}/api/v1/instagram/v1/fetch_user_info_by_username_v3',
        params={'username': username},
        headers={'Authorization': f'Bearer {api_key}'},
        timeout=30,
    )
    if not resp.ok:
        logger.error(f'[IG-TIKHUB] fetch_user_info {resp.status_code}: {resp.text[:300]}')
        return None

    body = resp.json()
    data = body.get('data') or {}
    if not data.get('status'):
        logger.warning(f'[IG-TIKHUB] fetch_user_info @{username}: status=false')
        return None

    logger.info(f'[IG-TIKHUB] fetch_user_info @{username}: ok (followers={data.get("follower_count")})')
    return data


def parse_instagram_user_info(username: str, info: dict) -> dict:
    """Parse kết quả fetch_user_info thành dict thông tin profile đầy đủ — thuần parse.

    BE ghi đè toàn bộ field này không điều kiện (khớp upsert_profile_from_user_info cũ).
    """
    canonical_url = f'https://www.instagram.com/{username}/'
    avatar = info.get('profile_pic_url') or ''
    hd_avatar = (info.get('hd_profile_pic_url_info') or {}).get('url') or ''
    bio_links_raw = info.get('bio_links') or []
    bio_links = bio_links_raw if isinstance(bio_links_raw, list) else []

    return {
        'url': canonical_url,
        'instagram_id': str(info.get('pk') or info.get('id') or '') or None,
        'full_name': info.get('full_name') or '',
        'avatar_url': avatar,
        'hd_avatar_url': hd_avatar,
        'biography': info.get('biography') or '',
        'bio_links': bio_links,
        'external_url': info.get('external_url') or '',
        'is_verified': bool(info.get('is_verified')),
        'is_private': bool(info.get('is_private')),
        'is_business': bool(info.get('is_business')),
        'category': info.get('category') or '',
        'followers_count': int(info.get('follower_count') or 0),
        'following_count': int(info.get('following_count') or 0),
        'posts_count': int(info.get('media_count') or 0),
    }


def fetch_instagram_reels(username: str, count: int = 100) -> list:
    """Fetch Instagram reels cho 1 user qua TikHub (có pagination).

    Args:
        username: Instagram username (không cần @)
        count: Số lượng reels tối đa cần lấy
    Returns:
        List of raw reel items (TikHub format)
    """
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")

    headers = {'Authorization': f'Bearer {api_key}'}

    items = []
    pagination_token = None

    # Chặn số trang, cùng lý do như get_user_posted_notes bên Xiaohongshu: điều kiện dừng
    # đếm số reel lấy được, nên tài khoản ít reel mà API cứ báo còn trang sau thì vòng lặp
    # lật mãi, mỗi trang một lượt TikHub tính phí.
    max_iterations = 8

    while len(items) < count and max_iterations > 0:
        max_iterations -= 1
        params: dict = {'username': username}
        if pagination_token:
            params['pagination_token'] = pagination_token

        resp = requests.get(
            f'{_tikhub_base()}/api/v1/instagram/v2/fetch_user_reels',
            params=params,
            headers=headers,
            timeout=30,
        )

        if not resp.ok:
            logger.error(f'[IG-TIKHUB] {resp.status_code}: {resp.text[:300]}')
            break

        body = resp.json()
        outer = body.get('data') or {}

        # TikHub có 2 format tuỳ endpoint version:
        # Format A (live): { data: { count, items[], pagination_token } }
        # Format B (docs):  { data: { data: { count, items[] }, pagination_token } }
        if 'items' in outer:
            page_items = outer.get('items') or []
            next_token = outer.get('pagination_token') or body.get('pagination_token')
        else:
            inner = outer.get('data') or {}
            page_items = inner.get('items') or []
            next_token = outer.get('pagination_token') or body.get('pagination_token')

        if not page_items:
            break

        items.extend(page_items)

        pagination_token = next_token
        if not pagination_token:
            break

    logger.info(f'[IG-TIKHUB] @{username}: fetched {len(items)} reels')
    return items[:count]


def _parse_datetime(val) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, (int, float)):
        from datetime import timezone as _tz
        return datetime.fromtimestamp(val, tz=_tz.utc)
    try:
        return datetime.fromisoformat(str(val).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def parse_instagram_item_user(item: dict) -> Optional[dict]:
    """Parse user object của 1 reel (fallback khi không có user info) — thuần parse.

    fetch_user_reels không trả về follower/following/posts_count trong user{}.
    Chỉ có url, avatar, is_verified — không có counts. Ưu tiên dùng parse_instagram_user_info
    nếu có; đây chỉ là fallback nhẹ luôn được BE áp dụng thêm sau đó (khớp code gốc).
    """
    user = item.get('user') or {}
    username = user.get('username') or ''
    if not username:
        return None

    return {
        'username': username,
        'url': f'https://www.instagram.com/{username}/',
        'avatar_url': user.get('profile_pic_url') or '',
        'is_verified': bool(user.get('is_verified')),
    }


def parse_instagram_reels(items: list) -> list:
    """Parse TikHub Instagram reels thành list dict sẵn sàng để BE upsert.

    Thuần parse — không đụng DB. Sắp xếp theo taken_at giảm dần (mới nhất trước),
    khớp thứ tự xử lý gốc.
    """
    if not items:
        return []

    sorted_items = sorted(
        items,
        key=lambda r: r.get('taken_at') if isinstance(r.get('taken_at'), (int, float)) else 0,
        reverse=True,
    )

    parsed = []
    for item in sorted_items:
        post_id = str(item.get('id') or '')
        shortcode = item.get('code') or ''
        if not post_id or not shortcode:
            continue

        date_posted = _parse_datetime(item.get('taken_at_date') or item.get('taken_at'))
        if not date_posted:
            date_posted = timezone.now()

        caption_obj = item.get('caption') or {}
        if isinstance(caption_obj, dict):
            description = caption_obj.get('text') or ''
            raw_tags = caption_obj.get('hashtags') or []
            hashtags = [t.lstrip('#') for t in raw_tags if t]
        else:
            description = ''
            hashtags = re.findall(r'#(\S+)', description)

        duration = None
        raw_dur = item.get('video_duration')
        if raw_dur is not None:
            try:
                duration = float(raw_dur)
            except (ValueError, TypeError):
                pass

        thumbnail = item.get('thumbnail_url') or ''
        if not thumbnail:
            img_items = ((item.get('image_versions') or {}).get('items') or [])
            if img_items:
                thumbnail = img_items[0].get('url') or ''

        parsed.append({
            'post_id': post_id,
            'shortcode': shortcode,
            'url': f'https://www.instagram.com/reel/{shortcode}/',
            'description': description,
            'hashtags': hashtags,
            'thumbnail_url': thumbnail,
            'duration_seconds': duration,
            'is_paid_partnership': bool(item.get('is_paid_partnership')),
            'play_count': item.get('play_count') or item.get('ig_play_count') or 0,
            'likes_count': item.get('like_count') or 0,
            'comments_count': item.get('comment_count') or 0,
            'date_posted': date_posted.isoformat(),
        })

    logger.info(f'[IG-TIKHUB] Parsed {len(parsed)} reels')
    return parsed
