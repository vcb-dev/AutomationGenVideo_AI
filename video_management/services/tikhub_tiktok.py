"""TikHub TikTok video search client + parse (fetch-only, không đụng DB).

Thay thế BrightData cho flow search TikTok video by keyword.
TikHub API trả data realtime, không cần polling.
BE (Prisma) là nơi upsert vào scraper_tiktok_videos.
"""

import logging
import requests
from datetime import datetime, timezone as tz
from typing import Optional
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _tikhub_base() -> str:
    return f"{getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')}/api/v1/tiktok/app/v3"


def search_tiktok_by_keyword(
    keyword: str,
    count: int = 30,
    region: str = 'VN',
) -> list:
    """Search TikTok videos by keyword via TikHub API.

    TikHub trả max ~20 video/request, dùng cursor để phân trang.
    Hàm này tự loop cho đến khi đủ `count` hoặc hết kết quả.
    """
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    all_videos = []
    cursor = 0
    max_iterations = 10  # tối đa 10 lần gọi API

    logger.info(f"[TIKHUB] Searching keyword='{keyword}' count={count} region={region}")

    for iteration in range(max_iterations):
        params = {
            "keyword": keyword,
            "count": min(20, count - len(all_videos)),
            "cursor": cursor,
            "region": region,
        }

        try:
            resp = requests.get(
                f"{_tikhub_base()}/fetch_video_search_result",
                params=params,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as e:
            logger.error(f"[TIKHUB] Request failed: {e}")
            break

        if not resp.ok:
            logger.error(f"[TIKHUB] API returned {resp.status_code}: {resp.text[:500]}")
            break

        body = resp.json()
        if body.get('code') != 200:
            logger.error(f"[TIKHUB] API error: {body.get('message', '')}")
            break

        data = body.get('data', {})
        search_items = data.get('search_item_list') or []

        for item in search_items:
            aweme = item.get('aweme_info')
            if aweme:
                all_videos.append(aweme)

        logger.info(f"[TIKHUB] Iteration {iteration+1}: got {len(search_items)} items, total={len(all_videos)}")

        if len(all_videos) >= count:
            break
        if not data.get('has_more'):
            break

        cursor = data.get('cursor', cursor + 20)

    logger.info(f"[TIKHUB] Total: {len(all_videos)} videos for keyword='{keyword}'")
    return all_videos[:count]


def _parse_cover_url(video_data: dict) -> str:
    """Extract thumbnail URL from TikHub video.cover object."""
    cover = video_data.get('cover', {})
    url_list = cover.get('url_list', [])
    for url in url_list:
        if url and '.jpeg' in url or '.jpg' in url or '.webp' in url:
            return url
    return url_list[0] if url_list else ''


def _parse_avatar_url(author: dict) -> str:
    """Extract avatar URL from author object."""
    avatar = author.get('avatar_medium', {}) or author.get('avatar_thumb', {})
    url_list = avatar.get('url_list', [])
    for url in url_list:
        if url and '.jpeg' in url or '.jpg' in url:
            return url
    return url_list[0] if url_list else ''


def parse_tiktok_videos(videos: list, search_keyword: str = '') -> list:
    """Parse TikHub aweme_info list thành list dict sẵn sàng để BE upsert.

    Thuần parse — không đụng DB. Field 'search_keyword' luôn là keyword của lần
    fetch hiện tại; BE tự quyết định giữ nguyên keyword cũ nếu video đã tồn tại
    (không ghi đè), và giữ nguyên Drive URL nếu preview_image đã là Drive URL.
    """
    parsed = []
    for v in videos:
        aweme_id = v.get('aweme_id') or ''
        if not aweme_id:
            continue

        create_time = v.get('create_time', 0)
        date_posted = datetime.fromtimestamp(create_time, tz=tz.utc) if create_time else timezone.now()

        cha_list = v.get('cha_list') or []
        hashtags = [c.get('cha_name', '') for c in cha_list if c.get('cha_name')]

        author = v.get('author', {})
        stats = v.get('statistics', {})
        music = v.get('music', {})
        video_info = v.get('video', {})

        share_info = v.get('share_info', {})
        share_url = share_info.get('share_url', '')
        unique_id = author.get('unique_id', '')
        post_id_str = str(aweme_id)
        url = share_url or f"https://www.tiktok.com/@{unique_id}/video/{post_id_str}"

        parsed.append({
            'post_id': post_id_str,
            'shortcode': post_id_str,
            'url': url,
            'description': v.get('desc') or '',
            'hashtags': hashtags,
            'thumbnail_url': _parse_cover_url(video_info),
            'video_duration': round((video_info.get('duration', 0) or 0) / 1000),
            'region': v.get('region') or '',
            'author_id': str(author.get('uid') or ''),
            'author_username': unique_id,
            'author_display_name': author.get('nickname') or '',
            'author_avatar': _parse_avatar_url(author),
            'author_url': f"https://www.tiktok.com/@{unique_id}" if unique_id else '',
            'author_followers': author.get('follower_count', 0) or 0,
            'author_is_verified': bool(author.get('custom_verify') or author.get('enterprise_verify_reason')),
            'play_count': stats.get('play_count', 0) or 0,
            'digg_count': stats.get('digg_count', 0) or 0,
            'comment_count': stats.get('comment_count', 0) or 0,
            'share_count': stats.get('share_count', 0) or 0,
            'collect_count': stats.get('collect_count', 0) or 0,
            'music_title': music.get('title') or '',
            'music_author': music.get('author') or '',
            'search_keyword': search_keyword,
            'date_posted': date_posted.isoformat(),
        })

    logger.info(f"[TIKHUB] Parsed {len(parsed)} videos (keyword='{search_keyword}')")
    return parsed
