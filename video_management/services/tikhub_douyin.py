"""TikHub Douyin video search client + DB ingest.

Flow: keyword → TikHub fetch_video_search_v2 (cursor pagination) → upsert DouyinVideo.
Pagination dùng 3 tham số: cursor + search_id + backtrace.
"""

import logging
import requests
from datetime import datetime, timezone as tz
from django.conf import settings
from django.utils import timezone

from ..models_scraper import DouyinVideo

logger = logging.getLogger(__name__)

TIKHUB_BASE_URL = "https://api.tikhub.io/api/v1/douyin/search"


def _parse_cover(video_obj: dict) -> str:
    for key in ('cover', 'origin_cover', 'dynamic_cover'):
        obj = video_obj.get(key) or {}
        url_list = obj.get('url_list') or []
        if url_list:
            return url_list[0]
    return ''


def _parse_avatar(author: dict) -> str:
    for key in ('avatar_medium', 'avatar_larger', 'avatar_thumb'):
        obj = author.get(key) or {}
        url_list = obj.get('url_list') or []
        if url_list:
            return url_list[0]
    return ''


def fetch_douyin_videos(keyword: str, count: int = 30) -> list:
    """Fetch Douyin videos via TikHub keyword search (cursor pagination).

    Args:
        keyword: Từ khóa tìm kiếm
        count: Số lượng tối đa cần lấy
    Returns:
        List aweme_info objects
    """
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    all_items = []
    cursor = 0
    search_id = ""
    backtrace = ""
    max_iterations = 20
    empty_pages = 0

    logger.info(f"[DOUYIN] Fetching keyword='{keyword}' count={count}")

    for iteration in range(max_iterations):
        payload = {
            "keyword": keyword,
            "cursor": cursor,
            "sort_type": "0",      # 0 = Tổng hợp
            "publish_time": "0",   # 0 = Không giới hạn
            "filter_duration": "0",
            "content_type": "1",   # 1 = Video
            "search_id": search_id,
            "backtrace": backtrace,
        }

        try:
            resp = requests.post(
                f"{TIKHUB_BASE_URL}/fetch_video_search_v2",
                json=payload,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"[DOUYIN] Request failed: {e}")
            break

        body = resp.json()
        if body.get('code') != 200:
            logger.error(f"[DOUYIN] API error: {body.get('message', '')}")
            break

        data = body.get('data', {})
        business_data = data.get('business_data') or []

        # Log phân phối type để debug
        type_counts: dict = {}
        for entry in business_data:
            t = entry.get('type')
            type_counts[t] = type_counts.get(t, 0) + 1
        logger.info(f"[DOUYIN] Page {iteration + 1}: business_data={len(business_data)} entries, types={type_counts}")

        items = []
        for entry in business_data:
            if entry.get('type') != 1:
                continue
            aweme_info = (entry.get('data') or {}).get('aweme_info')
            if aweme_info:
                items.append(aweme_info)

        all_items.extend(items)

        # Pagination nằm trong business_config:
        #   has_more   → biz_cfg['has_more']
        #   cursor     → biz_cfg['next_page']['cursor']
        #   search_id  → biz_cfg['next_page']['search_id']
        #   backtrace  → biz_cfg['backtrace']
        biz_cfg  = data.get('business_config') or {}
        next_page_obj = biz_cfg.get('next_page') or {}

        has_more    = bool(biz_cfg.get('has_more'))
        next_cursor = next_page_obj.get('cursor')
        search_id   = str(next_page_obj.get('search_id') or '')
        backtrace   = str(biz_cfg.get('backtrace') or '')

        logger.info(
            f"[DOUYIN] Page {iteration + 1}: +{len(items)} videos, total={len(all_items)} | "
            f"has_more={has_more} cursor={next_cursor!r}"
        )

        if len(all_items) >= count:
            break

        if not has_more or next_cursor is None:
            logger.info(f"[DOUYIN] Stopping: has_more={has_more}, cursor={next_cursor!r}")
            break

        # Nếu page không có video nào (chỉ có quảng cáo/user entries), vẫn tiếp tục
        # nhưng nếu liên tiếp 3 page trống thì dừng để tránh vòng lặp vô hạn
        if not items:
            empty_pages += 1
            if empty_pages >= 3:
                logger.warning(f"[DOUYIN] 3 consecutive empty pages, stopping.")
                break

        cursor = next_cursor

    logger.info(f"[DOUYIN] Done: {len(all_items)} items for '{keyword}'")
    return all_items[:count]


def fetch_douyin_user_videos(sec_user_id: str, count: int = 30) -> list:
    """Fetch videos từ trang cá nhân Douyin qua TikHub (max_cursor pagination).

    Args:
        sec_user_id: sec_user_id của tài khoản Douyin
        count: Số lượng video tối đa cần lấy
    Returns:
        List aweme_info objects (cùng format với keyword search)
    """
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    all_items = []
    max_cursor = 0
    max_iterations = 20

    logger.info(f"[DOUYIN PROFILE] Fetching sec_user_id='{sec_user_id[:30]}...' count={count}")

    for iteration in range(max_iterations):
        params = {
            "sec_user_id": sec_user_id,
            "max_cursor": max_cursor,
            "count": 20,
            "sort_type": "0",
        }
        try:
            resp = requests.get(
                "https://api.tikhub.io/api/v1/douyin/app/v3/fetch_user_post_videos",
                params=params,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"[DOUYIN PROFILE] Request failed: {e}")
            break

        body = resp.json()
        if body.get('code') != 200:
            logger.error(f"[DOUYIN PROFILE] API error: {body.get('message', '')}")
            break

        data = body.get('data', {})
        aweme_list = data.get('aweme_list') or []
        has_more = bool(data.get('has_more'))
        next_cursor = data.get('max_cursor', 0)

        logger.info(
            f"[DOUYIN PROFILE] Page {iteration + 1}: +{len(aweme_list)} videos, total={len(all_items) + len(aweme_list)} | "
            f"has_more={has_more} max_cursor={next_cursor}"
        )

        all_items.extend(aweme_list)

        if len(all_items) >= count:
            break

        if not has_more or not next_cursor:
            break

        max_cursor = next_cursor

    logger.info(f"[DOUYIN PROFILE] Done: {len(all_items)} items for '{sec_user_id[:30]}'")
    return all_items[:count]


def _is_drive_url(url: str) -> bool:
    return 'drive.google.com' in (url or '') or 'googleusercontent.com' in (url or '')


def ingest_douyin_videos(items: list, search_keyword: str) -> dict:
    """Parse TikHub Douyin aweme_info list và upsert vào DouyinVideo.

    - Lưu CDN cover URL trước, Drive upload chạy async qua Celery task.
    - video.duration là milliseconds → chia 1000 để ra giây.
    """
    if not items:
        return {'created': 0, 'updated': 0, 'skipped': 0}

    # Pre-fetch existing records kèm preview_image để tránh ghi đè Drive URL
    batch_ids = [str(item.get('aweme_id', '')) for item in items if item.get('aweme_id')]
    existing_data: dict[str, str] = {
        row['post_id']: row['preview_image']
        for row in DouyinVideo.objects.filter(post_id__in=batch_ids).values('post_id', 'preview_image')
    }
    existing_ids = set(existing_data.keys())

    created = 0
    updated = 0
    skipped = 0

    for item in items:
        aweme_id = str(item.get('aweme_id') or '')
        if not aweme_id:
            skipped += 1
            continue

        create_time = item.get('create_time') or 0
        date_posted = datetime.fromtimestamp(create_time, tz=tz.utc) if create_time else timezone.now()

        author = item.get('author') or {}
        music = item.get('music') or {}
        video_obj = item.get('video') or {}
        stats = item.get('statistics') or {}
        cha_list = item.get('cha_list') or []

        duration_ms = int(video_obj.get('duration') or 0)
        duration_sec = round(duration_ms / 1000) if duration_ms else 0

        hashtags = [c.get('cha_name', '') for c in cha_list if c.get('cha_name')]
        cover_url = _parse_cover(video_obj)

        uid = str(author.get('uid') or '')
        short_id = author.get('short_id') or author.get('unique_id') or ''
        nickname = author.get('nickname') or ''

        existing_preview = existing_data.get(aweme_id, '')
        has_drive = _is_drive_url(existing_preview)

        defaults = {
            'shortcode': aweme_id,
            'url': item.get('share_url') or f'https://www.douyin.com/video/{aweme_id}',
            'description': item.get('desc') or '',
            'hashtags': hashtags,
            # Giữ nguyên Drive URL nếu đã có, không ghi đè bằng CDN URL
            'preview_image': existing_preview if has_drive else cover_url,
            'video_duration': duration_sec,
            'region': item.get('region') or author.get('region') or '',
            'author_id': uid,
            'author_username': short_id,
            'author_display_name': nickname,
            'author_avatar': _parse_avatar(author),
            'author_followers': int(author.get('follower_count') or 0),
            'author_is_verified': bool(author.get('is_verified') or author.get('enterprise_verify_reason')),
            'digg_count': int(stats.get('digg_count') or 0),
            'comment_count': int(stats.get('comment_count') or 0),
            'share_count': int(stats.get('share_count') or 0),
            'collect_count': int(stats.get('collect_count') or 0),
            'music_title': music.get('title') or '',
            'music_author': music.get('author') or '',
            'search_keyword': search_keyword,
            'date_posted': date_posted,
        }

        obj, was_created = DouyinVideo.objects.update_or_create(
            post_id=aweme_id,
            defaults=defaults,
        )

        if was_created:
            created += 1
            existing_data[aweme_id] = cover_url
        else:
            updated += 1

        # Upload thumbnail: video mới hoặc video trùng chưa có Drive URL
        if cover_url and not has_drive:
            from ..tasks import upload_thumbnail_to_drive_task
            upload_thumbnail_to_drive_task.delay(
                model='douyin_video',
                object_id=obj.id,
                cdn_url=cover_url,
                filename=f'douyin-{aweme_id}.jpg',
            )

    logger.info(f"[DOUYIN] Ingest '{search_keyword}': +{created} new, ~{updated} updated, {skipped} skipped")
    return {'created': created, 'updated': updated, 'skipped': skipped}
