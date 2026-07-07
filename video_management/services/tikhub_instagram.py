"""TikHub Instagram Reels scraper + DB ingest logic.

Flow: User nhập username → TikHub cào reels (paginated) → upsert InstagramProfile + bulk InstagramReel.
"""

import logging
import re
import requests
from datetime import datetime
from typing import Optional
from django.conf import settings
from django.utils import timezone

from ..models_scraper import InstagramProfile, InstagramReel, InstagramProfileMetrics

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


def upsert_profile_from_user_info(username: str, info: dict) -> 'InstagramProfile':
    """Upsert InstagramProfile từ kết quả fetch_user_info.

    Cập nhật đầy đủ: follower counts, bio, category, v.v.
    """
    canonical_url = f'https://www.instagram.com/{username}/'
    avatar = info.get('profile_pic_url') or ''
    hd_avatar = (info.get('hd_profile_pic_url_info') or {}).get('url') or ''
    bio_links_raw = info.get('bio_links') or []
    bio_links = bio_links_raw if isinstance(bio_links_raw, list) else []

    fields = {
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

    profile, created = InstagramProfile.objects.get_or_create(
        username=username,
        defaults={'url': canonical_url},
    )

    for attr, val in fields.items():
        setattr(profile, attr, val)

    update_fields = list(fields.keys()) + ['updated_at']
    profile.save(update_fields=update_fields)

    logger.info(f'[IG-TIKHUB] {"Created" if created else "Updated"} profile from user_info: @{username}')
    return profile


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

    while len(items) < count:
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


def upsert_profile_from_item(item: dict) -> Optional[InstagramProfile]:
    """Upsert InstagramProfile từ user object của 1 reel (fallback khi không có user info).

    fetch_user_reels không trả về follower/following/posts_count trong user{}.
    Chỉ set url, avatar, is_verified — không ghi đè counts đã có.
    Ưu tiên dùng upsert_profile_from_user_info nếu có.
    """
    user = item.get('user') or {}
    username = user.get('username') or ''
    if not username:
        return None

    url = f'https://www.instagram.com/{username}/'
    avatar = user.get('profile_pic_url') or ''
    is_verified = bool(user.get('is_verified'))

    profile, created = InstagramProfile.objects.get_or_create(
        username=username,
        defaults={'url': url, 'avatar_url': avatar, 'is_verified': is_verified},
    )

    if not created:
        profile.url = url
        if avatar:
            profile.avatar_url = avatar
        profile.is_verified = is_verified
        profile.save(update_fields=['url', 'avatar_url', 'is_verified', 'updated_at'])

    logger.info(f'[IG-TIKHUB] {"Created" if created else "Updated"} profile from reel: @{username}')
    return profile


def ingest_instagram_reels(items: list) -> dict:
    """Parse TikHub Instagram reels và lưu vào DB.

    1. Upsert InstagramProfile từ reel mới nhất
    2. Upsert InstagramReel cho mỗi item
    3. Tạo metrics snapshot

    Returns: {'profile_id': int, 'created': int, 'updated': int, 'skipped': int}
    """
    if not items:
        return {'profile_id': None, 'created': 0, 'updated': 0, 'skipped': 0}

    sorted_items = sorted(
        items,
        key=lambda r: r.get('taken_at') if isinstance(r.get('taken_at'), (int, float)) else 0,
        reverse=True,
    )

    profile = upsert_profile_from_item(sorted_items[0])
    if not profile:
        logger.error('[IG-TIKHUB] Cannot extract profile info from items')
        return {'profile_id': None, 'created': 0, 'updated': 0, 'skipped': 0}

    InstagramProfileMetrics.objects.create(
        profile=profile,
        followers_count=profile.followers_count,
        following_count=profile.following_count,
        posts_count=profile.posts_count,
    )

    created = updated = skipped = 0

    for item in sorted_items:
        post_id = str(item.get('id') or '')
        shortcode = item.get('code') or ''
        if not post_id or not shortcode:
            skipped += 1
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

        defaults = {
            'profile': profile,
            'url': f'https://www.instagram.com/reel/{shortcode}/',
            'description': description,
            'hashtags': hashtags,
            'thumbnail_url': thumbnail,
            'duration_seconds': duration,
            'is_paid_partnership': bool(item.get('is_paid_partnership')),
            'play_count': item.get('play_count') or item.get('ig_play_count') or 0,
            'likes_count': item.get('like_count') or 0,
            'comments_count': item.get('comment_count') or 0,
            'date_posted': date_posted,
        }

        obj, was_created = InstagramReel.objects.update_or_create(
            post_id=post_id,
            defaults={'shortcode': shortcode, **defaults},
        )
        if was_created:
            created += 1
        else:
            updated += 1

    logger.info(f'[IG-TIKHUB] @{profile.username}: +{created} new, ~{updated} updated, {skipped} skipped')
    return {'profile_id': profile.id, 'created': created, 'updated': updated, 'skipped': skipped}
