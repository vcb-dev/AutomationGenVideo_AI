"""TikHub Bilibili profile + video scraper — fetch + parse only (không đụng DB).

Flow: mid (numeric user ID) → TikHub fetch_user_info (thông tin profile) +
fetch_user_videos (danh sách video, page-based pagination) → parse thành dict.
BE (Prisma) là nơi upsert vào scraper_bilibili_profiles/scraper_bilibili_videos.

Khác Kuaishou: Bilibili chỉ có 1 loại ID (mid, numeric) dùng xuyên suốt cho cả
fetch_user_info lẫn fetch_user_videos — không có vấn đề 2 không gian ID.

Lưu ý: fetch_user_videos KHÔNG trả like/comment/share/collect count (chỉ có
play + danmaku) — khác các nền tảng khác. fetch_general_search (search keyword)
thì có đủ play/like/favorites/danmaku + 1 trong 2 field video_review/review mơ
hồ ý nghĩa (comment count) — dùng video_review làm comment_count theo quy ước
phổ biến của API Bilibili công khai.
"""

import logging
import re
from datetime import datetime, timezone as tz
from typing import Optional
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _tikhub_base() -> str:
    return f"{getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')}/api/v1/bilibili"


def _headers() -> dict:
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")
    return {"Authorization": f"Bearer {api_key}"}


def _strip_em_tags(text: str) -> str:
    """Bỏ tag <em class="keyword">...</em> mà TikHub chèn để highlight từ khóa."""
    return re.sub(r'</?em[^>]*>', '', text or '')


def _extract_hashtags(tag_field: str) -> list:
    """tag field của Bilibili là chuỗi phân cách bằng dấu phẩy, KHÔNG có dấu '#'."""
    return [t.strip() for t in (tag_field or '').split(',') if t.strip()]


def _normalize_pic_url(url: str) -> str:
    """cover/pic có thể ở dạng protocol-relative '//i1.hdslb.com/...'."""
    if not url:
        return ''
    return f'https:{url}' if url.startswith('//') else url


def _parse_search_duration(duration_str: str) -> int:
    """duration của fetch_general_search là chuỗi 'M:SS' (phút không giới hạn,
    giây không pad — vd '8:8', '97:28' = 97 phút 28 giây). Trả về tổng giây."""
    if not duration_str:
        return 0
    parts = duration_str.split(':')
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        pass
    return 0


# ─── Fetch ────────────────────────────────────────────────────────────────────

def fetch_user_info(mid: str) -> Optional[dict]:
    """Gọi fetch_user_info, trả về data.data.card dict thô hoặc None nếu lỗi."""
    try:
        resp = requests.get(
            f'{_tikhub_base()}/app/fetch_user_info',
            params={'user_id': mid},
            headers=_headers(),
            timeout=30,
        )
        if not resp.ok:
            logger.error(f'[BILIBILI] fetch_user_info {resp.status_code}: {resp.text[:300]}')
            return None
        body = resp.json()
        data = body.get('data') or {}
        if data.get('code') != 0:
            logger.error(f'[BILIBILI] fetch_user_info API error: {data.get("message")}')
            return None
        inner = data.get('data') or {}
        return {
            'card': inner.get('card') or {},
            'archive_count': (inner.get('archive') or {}).get('count') or 0,
        }
    except requests.RequestException as e:
        logger.error(f'[BILIBILI] fetch_user_info request failed: {e}')
        return None


def fetch_user_videos(mid: str, count: int = 20) -> list:
    """Fetch video của 1 user qua TikHub (page-based pagination, post_filter=archive)."""
    all_videos: list = []
    page = 1
    page_size = 20
    max_iterations = 20

    for _ in range(max_iterations):
        if len(all_videos) >= count:
            break

        params = {
            'user_id': mid,
            'post_filter': 'archive',
            'page': page,
            'ps': page_size,
        }

        try:
            resp = requests.get(
                f'{_tikhub_base()}/app/fetch_user_videos',
                params=params,
                headers=_headers(),
                timeout=30,
            )
        except requests.RequestException as e:
            logger.error(f'[BILIBILI] fetch_user_videos request failed: {e}')
            break

        if not resp.ok:
            logger.error(f'[BILIBILI] fetch_user_videos {resp.status_code}: {resp.text[:300]}')
            break

        body = resp.json()
        data = body.get('data') or {}
        if data.get('code') != 0:
            logger.error(f'[BILIBILI] fetch_user_videos API error: {data.get("message")}')
            break

        inner = data.get('data') or {}
        page_items = inner.get('item') or []
        if not page_items:
            break

        all_videos.extend(page_items)

        total_count = inner.get('count') or 0
        if len(all_videos) >= total_count or len(page_items) < page_size:
            break
        page += 1

    return all_videos[:count]


def search_bilibili_by_keyword(keyword: str, count: int = 30, cursor: Optional[int] = None) -> tuple:
    """Search video theo keyword qua TikHub fetch_general_search (page-based pagination).

    Nhận `cursor` (số trang bắt đầu, 1-based) để nối tiếp từ lần gọi trước —
    dùng khi BE dedup làm giảm số video mới thực sự ingest được so với `count`.

    Returns: (videos, next_cursor, has_more)
    """
    all_items: list = []
    page = cursor or 1
    page_size = 42
    max_iterations = 20
    has_more = True

    logger.info(f"[BILIBILI] Searching keyword='{keyword}' count={count} start_page={page}")

    for _ in range(max_iterations):
        if len(all_items) >= count:
            break

        params = {
            'keyword': keyword,
            'order': 'totalrank',
            'page': page,
            'page_size': page_size,
        }

        try:
            resp = requests.get(
                f'{_tikhub_base()}/web/fetch_general_search',
                params=params,
                headers=_headers(),
                timeout=30,
            )
        except requests.RequestException as e:
            logger.error(f'[BILIBILI] fetch_general_search request failed: {e}')
            has_more = False
            break

        if not resp.ok:
            logger.error(f'[BILIBILI] fetch_general_search {resp.status_code}: {resp.text[:300]}')
            has_more = False
            break

        body = resp.json()
        data = body.get('data') or {}
        if data.get('code') != 0:
            logger.error(f'[BILIBILI] fetch_general_search API error: {data.get("message")}')
            has_more = False
            break

        inner = data.get('data') or {}
        # Bỏ quảng cáo (type != 'video') lẫn trong kết quả — field gần như rỗng hết.
        page_items = [r for r in (inner.get('result') or []) if r.get('type') == 'video']
        if not page_items:
            has_more = False
            break

        all_items.extend(page_items)

        num_pages = inner.get('numPages') or 0
        has_more = page < num_pages
        page += 1  # luôn tăng để trỏ đúng trang resume tiếp theo
        if len(all_items) >= count or not has_more:
            break

    logger.info(f"[BILIBILI] Total: {len(all_items)} videos for keyword='{keyword}', next_page={page}, has_more={has_more}")
    return all_items[:count], page, has_more


# ─── Parse ────────────────────────────────────────────────────────────────────

def parse_bilibili_profile(data: dict, mid_fallback: str = '') -> Optional[dict]:
    """Parse response fetch_user_info thành dict thô (không ghi DB)."""
    card = (data or {}).get('card') or {}
    mid = str(card.get('mid') or mid_fallback)
    if not mid:
        return None

    official_verify = card.get('official_verify') or {}
    is_verified = official_verify.get('type', -1) != -1

    return {
        'mid': mid,
        'username': card.get('handle') or '',
        'nickname': (card.get('name') or '')[:500],
        'url': f'https://space.bilibili.com/{mid}',
        'avatar_url': card.get('face') or '',
        'biography': card.get('sign') or '',
        'is_verified': is_verified,
        'verify_desc': (official_verify.get('title') or official_verify.get('desc') or '')[:255],
        'followers_count': int(card.get('fans') or 0),
        'following_count': int(card.get('attention') or 0),
        'likes_count': int((card.get('likes') or {}).get('like_num') or 0),
        'videos_count': int((data or {}).get('archive_count') or 0),
    }


def parse_bilibili_videos(items: list) -> list:
    """Parse list item thô từ fetch_user_videos thành list dict (không ghi DB)."""
    parsed = []
    for it in items:
        bvid = it.get('bvid') or ''
        if not bvid:
            continue

        ctime = it.get('ctime') or 0
        date_posted = datetime.fromtimestamp(ctime, tz=tz.utc) if ctime else datetime.now(tz.utc)

        parsed.append({
            'post_id': bvid,
            'url': f'https://www.bilibili.com/video/{bvid}',
            'description': (it.get('title') or '')[:2000],
            'thumbnail_url': _normalize_pic_url(it.get('cover') or ''),
            'video_duration': int(it.get('duration') or 0),
            'view_count': int(it.get('play') or 0),
            'danmaku_count': int(it.get('danmaku') or 0),
            'date_posted': date_posted.isoformat(),
        })
    return parsed


def parse_bilibili_search_videos(items: list, search_keyword: str = '') -> list:
    """Parse list result thô từ fetch_general_search thành list dict (không ghi DB)."""
    parsed = []
    for r in items:
        bvid = r.get('bvid') or ''
        if not bvid:
            continue

        title = _strip_em_tags(r.get('title') or '')
        pubdate = r.get('pubdate') or 0
        date_posted = datetime.fromtimestamp(pubdate, tz=tz.utc) if pubdate else datetime.now(tz.utc)

        parsed.append({
            'post_id': bvid,
            'url': f'https://www.bilibili.com/video/{bvid}',
            'description': title[:2000],
            'hashtags': _extract_hashtags(r.get('tag') or ''),
            'thumbnail_url': _normalize_pic_url(r.get('pic') or ''),
            'video_duration': _parse_search_duration(r.get('duration') or ''),
            'author_id': str(r.get('mid') or ''),
            'author_username': r.get('author') or '',
            'author_avatar': r.get('upic') or '',
            'view_count': int(r.get('play') or 0),
            'like_count': int(r.get('like') or 0),
            'comment_count': int(r.get('video_review') or 0),
            'collect_count': int(r.get('favorites') or 0),
            'danmaku_count': int(r.get('danmaku') or 0),
            'search_keyword': search_keyword,
            'date_posted': date_posted.isoformat(),
        })
    return parsed
