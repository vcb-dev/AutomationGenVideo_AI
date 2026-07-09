"""TikHub Xiaohongshu video search + user profile fetch + parse (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi ScraperXiaohongshuVideo/ScraperXiaohongshuProfile.
AI chỉ gọi TikHub + parse dữ liệu, trả dict thô cho BE tự lưu.
"""

import logging
import requests
from datetime import datetime, timezone as _tz
from typing import Optional
from django.conf import settings

logger = logging.getLogger(__name__)


def _tikhub_base() -> str:
    return getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')


def _parse_duration(s: str) -> int:
    """'1:33' → 93, '0:28' → 28. Trả 0 nếu không parse được."""
    if not s:
        return 0
    parts = s.split(':')
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, TypeError):
        pass
    return 0


def _extract_thumbnail(note: dict) -> str:
    """Lấy thumbnail URL tốt nhất từ note object."""
    images = note.get('images_list') or []
    if images:
        img = images[0]
        return img.get('url_size_large') or img.get('url') or ''
    # fallback: first_frame từ video_info_v2
    vi = note.get('video_info_v2') or {}
    return (vi.get('image') or {}).get('first_frame') or ''


def search_xiaohongshu_videos(keyword: str, count: int = 20) -> list:
    """Tìm kiếm video notes trên Xiaohongshu qua TikHub (có pagination).

    Chỉ lấy video notes (note_type="视频笔记").
    Trả về list raw note objects từ TikHub.
    """
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError('TIKHUB_API_KEY not configured')

    headers = {'Authorization': f'Bearer {api_key}'}
    items = []
    page = 1
    search_id: Optional[str] = None
    search_session_id: Optional[str] = None

    while len(items) < count:
        params: dict = {
            'keyword': keyword,
            'page': page,
            'sort_type': 'general',
            'note_type': '视频笔记',
        }
        if search_id:
            params['search_id'] = search_id
            params['search_session_id'] = search_session_id

        resp = requests.get(
            f'{_tikhub_base()}/api/v1/xiaohongshu/app_v2/search_notes',
            params=params,
            headers=headers,
            timeout=30,
        )
        if not resp.ok:
            logger.error(f'[XHS] {resp.status_code}: {resp.text[:300]}')
            break

        body = resp.json()
        outer = (body.get('data') or {}).get('data') or {}

        search_id = outer.get('search_id')
        search_session_id = outer.get('search_session_id')
        next_page = outer.get('next_page')

        page_notes = [
            item['note']
            for item in (outer.get('items') or [])
            if item.get('model_type') == 'note'
            and (item.get('note') or {}).get('type') == 'video'
        ]

        if not page_notes:
            break

        items.extend(page_notes)

        if not next_page or next_page <= page:
            break
        page = next_page

    logger.info(f'[XHS] keyword="{keyword}": fetched {len(items)} video notes')
    return items[:count]


def parse_xiaohongshu_videos(notes: list) -> list:
    """Parse TikHub Xiaohongshu search notes thành list dict thô (không ghi DB).

    Returns: list of {'note_id', 'url', 'title', 'description', 'thumbnail_url',
    'author_id', 'author_name', 'author_avatar', 'duration_seconds', 'liked_count',
    'collected_count', 'comments_count', 'shared_count', 'date_posted'}
    """
    parsed = []

    for note in notes:
        note_id = note.get('id') or ''
        if not note_id:
            continue

        xsec_token = note.get('xsec_token') or ''
        url = f'https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_search'

        user = note.get('user') or {}
        thumbnail = _extract_thumbnail(note)

        ts = note.get('timestamp')
        if ts:
            try:
                date_posted = datetime.fromtimestamp(int(ts), tz=_tz.utc)
            except (ValueError, TypeError):
                date_posted = datetime.now(tz=_tz.utc)
        else:
            date_posted = datetime.now(tz=_tz.utc)

        parsed.append({
            'note_id': note_id,
            'url': url,
            'title': (note.get('title') or '')[:1000],
            'description': note.get('desc') or '',
            'thumbnail_url': thumbnail,
            'author_id': user.get('userid') or '',
            'author_name': (user.get('nickname') or '')[:500],
            'author_avatar': user.get('images') or '',
            'duration_seconds': _parse_duration(note.get('video_duration') or ''),
            'liked_count': int(note.get('liked_count') or 0),
            'collected_count': int(note.get('collected_count') or 0),
            'comments_count': int(note.get('comments_count') or 0),
            'shared_count': int(note.get('shared_count') or 0),
            'date_posted': date_posted.isoformat(),
        })

    logger.info(f'[XHS] parsed {len(parsed)}/{len(notes)} video notes')
    return parsed


# ═══════════════════════════════════════════════════════════
#  USER PROFILE — GET /api/v1/xiaohongshu/app_v2/get_user_posted_notes
# ═══════════════════════════════════════════════════════════

def fetch_xhs_user_video_notes(user_id: str, count: int = 100) -> list:
    """Cào video notes từ một user Xiaohongshu qua TikHub (cursor pagination).

    Chỉ trả về notes có type == 'video'.
    """
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError('TIKHUB_API_KEY not configured')

    headers = {'Authorization': f'Bearer {api_key}'}
    notes = []
    cursor: Optional[str] = None

    while len(notes) < count:
        params: dict = {'user_id': user_id}
        if cursor:
            params['cursor'] = cursor

        resp = requests.get(
            f'{_tikhub_base()}/api/v1/xiaohongshu/app_v2/get_user_posted_notes',
            params=params,
            headers=headers,
            timeout=30,
        )
        if not resp.ok:
            logger.error(f'[XHS-PROFILE] HTTP {resp.status_code}: {resp.text[:300]}')
            break

        body = resp.json()
        # TikHub trả code != 200 khi user_id không tồn tại hoặc lỗi API
        if body.get('code') not in (200, 0, None):
            logger.error(f'[XHS-PROFILE] TikHub error code={body.get("code")}: {body.get("msg") or body}')
            err_msg = body.get('msg') or f"code={body.get('code')}"
            raise ValueError(f'TikHub: {err_msg}')

        inner = (body.get('data') or {}).get('data') or {}
        page_notes = inner.get('notes') or []
        has_more = inner.get('has_more', False)

        # Lọc chỉ lấy video notes
        video_notes = [n for n in page_notes if n.get('type') == 'video']
        notes.extend(video_notes)

        if not has_more or not page_notes:
            break

        # cursor cho trang tiếp theo = cursor của note cuối cùng (bằng với note.id)
        cursor = str(page_notes[-1].get('cursor') or page_notes[-1].get('id') or '')
        if not cursor:
            break

    logger.info(f'[XHS-PROFILE] user_id={user_id}: fetched {len(notes)} video notes')
    return notes[:count]


def parse_xhs_profile_videos(notes: list) -> list:
    """Parse user-posted notes thành list dict thô (không ghi DB).

    User notes dùng field names khác search notes:
    - likes (không phải liked_count)
    - share_count (không phải shared_count)
    - create_time (unix seconds, không phải timestamp)
    - duration: video_info_v2.capa.duration (int seconds, không cần parse string)
    - URL: không có xsec_token
    """
    parsed = []

    for note in notes:
        note_id = note.get('id') or ''
        if not note_id:
            continue

        url = f'https://www.xiaohongshu.com/explore/{note_id}'

        user = note.get('user') or {}
        thumbnail = _extract_thumbnail(note)

        ts = note.get('create_time')
        if ts:
            try:
                date_posted = datetime.fromtimestamp(int(ts), tz=_tz.utc)
            except (ValueError, TypeError):
                date_posted = datetime.now(tz=_tz.utc)
        else:
            date_posted = datetime.now(tz=_tz.utc)

        # Duration từ video_info_v2.capa.duration (int seconds)
        vi = note.get('video_info_v2') or {}
        capa = vi.get('capa') or {}
        duration = 0
        raw_dur = capa.get('duration')
        if raw_dur is not None:
            try:
                duration = int(float(raw_dur))
            except (ValueError, TypeError):
                duration = 0

        title = (note.get('title') or note.get('display_title') or '')[:1000]

        parsed.append({
            'note_id': note_id,
            'url': url,
            'title': title,
            'description': note.get('desc') or '',
            'thumbnail_url': thumbnail,
            'author_id': user.get('userid') or '',
            'author_name': (user.get('nickname') or '')[:500],
            'author_avatar': user.get('images') or '',
            'duration_seconds': duration,
            'liked_count': int(note.get('likes') or note.get('liked_count') or 0),
            'collected_count': int(note.get('collected_count') or 0),
            'comments_count': int(note.get('comments_count') or 0),
            'shared_count': int(note.get('share_count') or note.get('shared_count') or 0),
            'date_posted': date_posted.isoformat(),
        })

    logger.info(f'[XHS-PROFILE] parsed {len(parsed)}/{len(notes)} video notes')
    return parsed


def parse_xhs_author(first_note_user: dict) -> dict:
    """Parse user object trong note trả về thành dict thô cho profile (không ghi DB).

    nickname/avatar_url để '' nếu thiếu — BE tự quyết định có ghi đè hay không
    (khớp hành vi update_or_create defaults cũ: chỉ set key khi có giá trị).
    """
    verify_type = first_note_user.get('red_official_verify_type', 0)
    return {
        'nickname': (first_note_user.get('nickname') or '')[:500],
        'avatar_url': first_note_user.get('images') or '',
        'is_verified': bool(verify_type),
    }
