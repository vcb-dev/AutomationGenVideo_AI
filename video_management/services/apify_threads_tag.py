"""Cào bài Threads theo TAG CHỦ ĐỀ (dòng "người đăng > trang sức" trên bài) qua Apify — fetch-only.

Khác tìm theo từ khoá (tikhub_threads.search_top_threads): Threads chỉ cho xem bảng tin của
một tag khi đã đăng nhập, TikHub không có endpoint tag và không trả tag về. Actor Apify cào
được bảng tin tag, kể cả bài gắn tag mà nội dung không nhắc chữ đó (kiểm chứng 2026-10-01 với
tag "trang sức": 19 bài, 7 bài không có chữ "trang sức" trong nội dung, $0.0386).

Actor tính phí theo bài trả về nên luôn chặn bằng maxItems = số bài người dùng yêu cầu.
"""

import logging
from datetime import datetime, timezone as tz
from typing import Any, List

import requests
from django.conf import settings

from .tikhub_threads import is_vietnamese_text

logger = logging.getLogger(__name__)

ACTOR_ENV = 'APIFY_ACTOR_THREADS_TAG'
BASE_URL_ENV = 'APIFY_API_BASE_URL'
RUN_TIMEOUT_SECONDS = 300


class ThreadsTagConfigError(Exception):
    """Thiếu biến cấu hình — tầng view trả 400 kèm tên biến."""


def _required_setting(name: str) -> str:
    value = str(getattr(settings, name, '') or '').strip()
    if not value:
        raise ThreadsTagConfigError(f'Thiếu biến môi trường {name}')
    return value


def normalize_tag(tag: str) -> str:
    """'#trang sức' / ' Trang Sức ' → 'trang sức'. Tag Threads không phân biệt hoa thường."""
    return ' '.join((tag or '').strip().lstrip('#').split()).lower()


def fetch_tag_posts(tag: str, count: int) -> List[dict]:
    """Gọi actor Apify lấy tối đa `count` bài của tag (raw item của actor)."""
    actor = _required_setting(ACTOR_ENV).replace('/', '~')
    token = _required_setting('APIFY_API_TOKEN')
    base_url = _required_setting(BASE_URL_ENV).rstrip('/')

    resp = requests.post(
        f'{base_url}/v2/acts/{actor}/run-sync-get-dataset-items',
        params={'timeout': RUN_TIMEOUT_SECONDS, 'maxItems': count},
        headers={'Authorization': f'Bearer {token}'},
        json={'hashtags': [tag], 'maxPosts': count},
        timeout=RUN_TIMEOUT_SECONDS + 30,
    )
    if not resp.ok:
        logger.error(f'[THREADS-TAG] Apify "{tag}" HTTP {resp.status_code}: {resp.text[:300]}')
        resp.raise_for_status()
    items = resp.json()
    if not isinstance(items, list):
        return []
    logger.info(f'[THREADS-TAG] tag "{tag}": {len(items)} bài (yêu cầu {count})')
    return items[:count]


def _first_media_url(media: Any) -> str:
    if not isinstance(media, list) or not media:
        return ''
    first = media[0]
    if isinstance(first, str):
        return first
    if isinstance(first, dict):
        return str(first.get('url') or '')
    return ''


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_tag_posts(items: List[dict], tag: str) -> List[dict]:
    """Chuẩn hoá item Apify về cùng dạng parse_threads_posts để BE lưu chung một bảng."""
    parsed = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        post_id = str(item.get('pk') or item.get('id') or '').strip()
        username = str(item.get('username') or '').strip()
        if not post_id or post_id in seen or not username:
            continue
        seen.add(post_id)

        text = str(item.get('text') or '')
        video_url = _first_media_url(item.get('videos'))
        thumbnail_url = _first_media_url(item.get('images'))
        if video_url:
            media_type = 'VIDEO'
        elif _to_int(item.get('image_count')) > 1:
            media_type = 'CAROUSEL'
        elif thumbnail_url:
            media_type = 'IMAGE'
        else:
            media_type = 'TEXT'

        published = item.get('published_on')
        try:
            date_posted = datetime.fromtimestamp(int(published), tz=tz.utc).isoformat()
        except (TypeError, ValueError):
            date_posted = datetime.now(tz.utc).isoformat()

        code = str(item.get('code') or '')
        parsed.append({
            'topic_tag': str(item.get('hashtag') or tag),
            'post_id': post_id,
            'shortcode': code,
            'url': str(item.get('url') or f'https://www.threads.net/@{username}/post/{code}'),
            'text': text,
            'media_type': media_type,
            'thumbnail_url': thumbnail_url,
            'video_url': video_url,
            # Actor không trả lượt xem / chia sẻ lại / trích dẫn.
            'views_count': 0,
            'likes_count': _to_int(item.get('like_count')),
            'replies_count': _to_int(item.get('reply_count')),
            'reposts_count': 0,
            'quotes_count': 0,
            'date_posted': date_posted,
            'author_username': username,
            'author_name': username,
            'author_avatar': str(item.get('user_pic') or ''),
            'is_vietnamese': is_vietnamese_text(text),
        })

    parsed.sort(key=lambda p: (1 if p['is_vietnamese'] else 0, p['likes_count']), reverse=True)
    return parsed
