"""TikHub TikTok Profile Posts scraper — fetch-only (không đụng DB).

Flow: username → TikHub fetch_user_post_videos_v3 (cursor pagination) → parse thành dict.
BE (Prisma) là nơi upsert vào scraper_tiktok_profiles/scraper_tiktok_profile_videos.
"""

import logging
from datetime import datetime, timezone as tz, timedelta
from typing import Optional
import requests
from django.conf import settings
from django.utils import timezone

from .tikhub_errors import raise_if_auth_exception

logger = logging.getLogger(__name__)


def _tikhub_base() -> str:
    return f"{getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')}/api/v1/tiktok/app/v3"


# ─── Fetch ────────────────────────────────────────────────────────────────────

def fetch_user_posts(
    unique_id: str,
    count: int = 50,
    days: Optional[int] = None,
    sec_uid: str = '',
) -> list:
    """Fetch TikTok user posts via TikHub (cursor pagination).

    Args:
        unique_id: TikTok username (không có @) — dùng làm fallback
        count: Số lượng tối đa cần lấy (dùng cho initial scrape)
        days: Nếu set, chỉ lấy posts trong N ngày gần nhất (bỏ qua count)
        sec_uid: sec_user_id — ưu tiên dùng thay unique_id, không đổi khi user đổi username
    Returns:
        List aweme items (raw TikHub format)
    """
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    cutoff_ts = None
    if days:
        cutoff_ts = (datetime.now(tz=tz.utc) - timedelta(days=days)).timestamp()

    all_items = []
    max_cursor = 0
    max_iterations = 20  # tối đa 20 pages × 20 items/page = 400 items

    identifier = f"sec_uid={sec_uid[:20]}..." if sec_uid else f"@{unique_id}"
    logger.info(f"[TT-PROFILE-TH] Fetching {identifier} count={count} days={days}")

    for iteration in range(max_iterations):
        # sec_uid ưu tiên cao hơn unique_id (nhanh hơn + ổn định hơn)
        params = {
            "count": 20,
            "max_cursor": max_cursor,
            "sort_type": 0,  # 0 = Latest
        }
        if sec_uid:
            params["sec_user_id"] = sec_uid
        else:
            params["unique_id"] = unique_id

        try:
            resp = requests.get(
                f"{_tikhub_base()}/fetch_user_post_videos_v3",
                params=params,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise_if_auth_exception(e, 'tiktok fetch_user_post_videos_v3')
            logger.error(f"[TT-PROFILE-TH] Request failed: {e}")
            break

        body = resp.json()
        if body.get('code') != 200:
            logger.error(f"[TT-PROFILE-TH] API error: {body.get('message', '')}")
            break

        data = body.get('data', {})
        items = data.get('aweme_list') or []

        if not items:
            break

        for item in items:
            create_time = item.get('create_time', 0) or 0

            # Nếu cào theo days và video cũ hơn cutoff → dừng
            if cutoff_ts and create_time and create_time < cutoff_ts:
                logger.info(f"[TT-PROFILE-TH] Reached cutoff at iteration {iteration+1}")
                return all_items

            all_items.append(item)

        logger.info(f"[TT-PROFILE-TH] Page {iteration+1}: +{len(items)}, total={len(all_items)}")

        # Nếu cào theo count và đủ rồi thì dừng
        if not days and len(all_items) >= count:
            break

        if not data.get('has_more'):
            break

        max_cursor = data.get('max_cursor', 0)
        if not max_cursor:
            break

    logger.info(f"[TT-PROFILE-TH] Total: {len(all_items)} items for @{unique_id}")

    if not days:
        return all_items[:count]
    return all_items


# ─── Parse helpers ────────────────────────────────────────────────────────────

def _parse_cover(video_obj: dict) -> str:
    cover = video_obj.get('cover') or video_obj.get('origin_cover') or {}
    url_list = cover.get('url_list') or []
    return url_list[0] if url_list else ''


def _parse_avatar(author: dict) -> str:
    for key in ('avatar_medium', 'avatar_larger', 'avatar_thumb'):
        obj = author.get(key) or {}
        url_list = obj.get('url_list') or []
        if url_list:
            return url_list[0]
    return ''


# ─── Parse profile ────────────────────────────────────────────────────────────

def parse_tiktok_author(author: dict) -> Optional[dict]:
    """Parse author object của TikHub response thành dict thông tin profile — thuần parse."""
    unique_id = author.get('unique_id', '')
    if not unique_id:
        return None

    uid = str(author.get('uid', '') or '')
    sec_uid = author.get('sec_uid', '') or ''

    return {
        'profile_id': uid,
        'username': unique_id,
        'sec_uid': sec_uid,
        'nickname': author.get('nickname') or '',
        'url': f'https://www.tiktok.com/@{unique_id}',
        'avatar_url': _parse_avatar(author),
        'biography': author.get('signature') or '',
        'is_verified': bool(author.get('custom_verify') or author.get('enterprise_verify_reason')),
        'followers_count': int(author.get('follower_count') or 0),
        'following_count': int(author.get('following_count') or 0),
        'videos_count': int(author.get('aweme_count') or 0),
    }


# ─── Parse videos ─────────────────────────────────────────────────────────────

def parse_tiktok_profile_videos(items: list, username: str = '') -> list:
    """Parse TikHub aweme_list thành list dict sẵn sàng để BE upsert vào TikTokProfileVideo.

    Thuần parse — không đụng DB. video.duration từ TikHub là milliseconds → chia 1000 để ra giây.
    """
    parsed = []
    for item in items:
        aweme_id = str(item.get('aweme_id') or '')
        if not aweme_id:
            continue

        create_time = item.get('create_time') or 0
        date_posted = datetime.fromtimestamp(create_time, tz=tz.utc) if create_time else timezone.now()

        cha_list = item.get('cha_list') or []
        hashtags = [c.get('cha_name', '') for c in cha_list if c.get('cha_name')]

        video_obj = item.get('video') or {}
        duration_ms = int(video_obj.get('duration') or 0)
        duration_sec = round(duration_ms / 1000) if duration_ms else 0

        music = item.get('music') or {}
        share_info = item.get('share_info') or {}
        stats = item.get('statistics') or {}

        parsed.append({
            'video_id': aweme_id,
            'shortcode': aweme_id,
            'url': share_info.get('share_url') or f'https://www.tiktok.com/@{username}/video/{aweme_id}',
            'description': item.get('desc') or '',
            'hashtags': hashtags,
            'thumbnail_url': _parse_cover(video_obj),
            'video_duration': duration_sec,
            'region': item.get('region') or '',
            'post_type': 'video',
            'play_count': int(stats.get('play_count') or 0),
            'digg_count': int(stats.get('digg_count') or 0),
            'comment_count': int(stats.get('comment_count') or 0),
            'share_count': int(stats.get('share_count') or 0),
            'favorites_count': int(stats.get('collect_count') or 0),
            'music_title': music.get('title') or '',
            'music_author': music.get('author') or '',
            'date_posted': date_posted.isoformat(),
        })

    logger.info(f"[TT-PROFILE-TH] Parsed {len(parsed)} videos for @{username}")
    return parsed
