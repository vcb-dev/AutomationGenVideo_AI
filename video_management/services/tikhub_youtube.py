"""TikHub YouTube channel + shorts scraper — fetch + parse only (không đụng DB).

Flow: channel_id → TikHub get_channel_info (thông tin kênh) + get_channel_shorts
(danh sách Shorts, continuation-token pagination) → parse thành dict.
BE (Prisma) là nơi upsert vào scraper_youtube_profiles/scraper_youtube_shorts.

get_channel_shorts dùng channel_id (không dùng channel_url — theo docs TikHub,
channel_url chỉ optional và bị ignore nếu đã có channel_id) + continuation_token
để phân trang, đúng usage flow: request đầu chỉ gửi channel_id, các request sau
gửi kèm continuation_token lấy từ response trước.
"""

import logging
import re
from datetime import datetime, timezone as tz
from typing import Optional
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _tikhub_base() -> str:
    return f"{getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')}/api/v1/youtube"


def _headers() -> dict:
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")
    return {"Authorization": f"Bearer {api_key}"}


# ─── Parse helpers ────────────────────────────────────────────────────────────

_COUNT_MULTIPLIERS = {
    'k': 1_000, 'thousand': 1_000,
    'm': 1_000_000, 'million': 1_000_000,
    'b': 1_000_000_000, 'billion': 1_000_000_000,
}

_COUNT_RE = re.compile(r'^([\d,]+\.?\d*)\s*(k|m|b|thousand|million|billion)?')


def _parse_count(text: str) -> int:
    """Parse số dạng YouTube trả về: '729K subscribers', '244,909,875 views',
    '4.1 thousand views', '57 videos' -> int thô.
    """
    if not text:
        return 0
    t = text.strip().lower()
    t = re.sub(r'\b(subscribers?|videos?|views?)\b', '', t).strip()
    m = _COUNT_RE.match(t)
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(',', ''))
    except ValueError:
        return 0
    suffix = m.group(2)
    if suffix:
        num *= _COUNT_MULTIPLIERS.get(suffix, 1)
    return int(num)


def _pick_largest(images: list) -> str:
    """Chọn ảnh có width lớn nhất trong list avatar/banner/thumbnails."""
    if not images:
        return ''
    best = max(images, key=lambda x: x.get('width', 0) or 0)
    return best.get('url', '') or ''


def _parse_creation_date(text: str) -> Optional[datetime]:
    """'Dec 3, 2016' -> datetime. Trả None nếu không parse được."""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), '%b %d, %Y').replace(tzinfo=tz.utc)
    except ValueError:
        return None


def _extract_hashtags(text: str) -> list:
    return [m.lstrip('#') for m in re.findall(r'#\w+', text or '')]


# ─── Fetch ────────────────────────────────────────────────────────────────────

def fetch_channel_info(channel_id: str) -> Optional[dict]:
    """Gọi get_channel_info, trả về dict data thô hoặc None nếu lỗi."""
    try:
        resp = requests.get(
            f'{_tikhub_base()}/web/get_channel_info',
            params={'channel_id': channel_id},
            headers=_headers(),
            timeout=30,
        )
        if not resp.ok:
            logger.error(f'[YOUTUBE] get_channel_info {resp.status_code}: {resp.text[:300]}')
            return None
        body = resp.json()
        if body.get('code') != 200:
            logger.error(f'[YOUTUBE] get_channel_info API error: {body.get("message")}')
            return None
        return body.get('data') or None
    except requests.RequestException as e:
        logger.error(f'[YOUTUBE] get_channel_info request failed: {e}')
        return None


def fetch_channel_shorts(channel_id: str, count: int = 20) -> list:
    """Fetch Shorts của 1 channel qua TikHub (continuation_token pagination).

    Docs TikHub: channel_id là tham số recommended; channel_url chỉ optional và
    bị ignore nếu đã truyền channel_id — nên luôn gửi channel_id trực tiếp.
    """
    all_shorts: list = []
    continuation_token: Optional[str] = None
    max_iterations = 20

    for _ in range(max_iterations):
        if len(all_shorts) >= count:
            break

        params: dict = {'channel_id': channel_id, 'need_format': 'true'}
        if continuation_token:
            params['continuation_token'] = continuation_token

        try:
            resp = requests.get(
                f'{_tikhub_base()}/web_v2/get_channel_shorts',
                params=params,
                headers=_headers(),
                timeout=30,
            )
        except requests.RequestException as e:
            logger.error(f'[YOUTUBE] get_channel_shorts request failed: {e}')
            break

        if not resp.ok:
            logger.error(f'[YOUTUBE] get_channel_shorts {resp.status_code}: {resp.text[:300]}')
            break

        body = resp.json()
        if body.get('code') != 200:
            logger.error(f'[YOUTUBE] get_channel_shorts API error: {body.get("message")}')
            break

        data = body.get('data') or {}
        page_shorts = data.get('shorts') or []
        if not page_shorts:
            break

        # Phòng vệ: nếu trang mới trùng trang cũ (tham số continuation_token không
        # được TikHub nhận ra) thì dừng luôn, tránh lặp vô hạn / trùng dữ liệu.
        if all_shorts and page_shorts[0].get('video_id') == all_shorts[0].get('video_id'):
            break

        all_shorts.extend(page_shorts)

        if not data.get('has_more') or not data.get('continuation_token'):
            break
        continuation_token = data.get('continuation_token')

    return all_shorts[:count]


# ─── Parse ────────────────────────────────────────────────────────────────────

def parse_youtube_channel(data: dict, channel_id_fallback: str = '') -> Optional[dict]:
    """Parse response get_channel_info thành dict thô (không ghi DB)."""
    channel_id = (data or {}).get('channel_id') or channel_id_fallback
    if not channel_id:
        return None

    created_at = _parse_creation_date(data.get('creation_date') or '')

    return {
        'channel_id': channel_id,
        'title': (data.get('title') or '')[:500],
        'description': data.get('description') or '',
        'url': f'https://www.youtube.com/channel/{channel_id}',
        'avatar_url': _pick_largest(data.get('avatar') or []),
        'banner_url': _pick_largest(data.get('banner') or []),
        'is_verified': bool(data.get('verified')),
        'has_business_email': bool(data.get('has_business_email')),
        'subscriber_count': _parse_count(data.get('subscriber_count') or ''),
        'video_count': _parse_count(data.get('video_count') or ''),
        'view_count': _parse_count(data.get('view_count') or ''),
        'country': (data.get('country') or '')[:100],
        'channel_created_at': created_at.isoformat() if created_at else None,
    }


def parse_youtube_shorts(shorts: list) -> list:
    """Parse list shorts thô thành list dict (không ghi DB)."""
    parsed = []
    for s in shorts:
        video_id = s.get('video_id') or ''
        if not video_id:
            continue
        title = s.get('title') or ''
        view_count_text = s.get('view_count_text') or ''

        parsed.append({
            'video_id': video_id,
            'title': title[:2000],
            'hashtags': _extract_hashtags(title),
            'url': s.get('video_url') or f'https://www.youtube.com/shorts/{video_id}',
            'thumbnail_url': _pick_largest(s.get('thumbnails') or []),
            'view_count': _parse_count(view_count_text),
            'view_count_text': view_count_text[:50],
        })
    return parsed
