"""Lấy chi tiết MỘT video theo (platform, video_id) qua TikHub — fetch-only, không đụng DB.

Dùng cho luồng "đề xuất video": người dùng lướt ngoài (Douyin, TikTok...), bấm đề xuất,
extension chỉ moi được mã video từ URL. Số liệu view/tim/bình luận phải lấy từ đây, vì đọc
thẳng trên trang không đáng tin (bảng tin nhúng JSON của nhiều video khác nhau, trang SPA
thì state nhúng đã cũ) — và đoán sai còn tệ hơn để trống.

Facebook KHÔNG có ở đây: TikHub không hỗ trợ Facebook (đã tra danh mục API, 0 endpoint).
Facebook trong hệ thống chạy qua RapidAPI và chỉ có endpoint cấp trang, không lấy được 1 video.

Mỗi nền tảng trả một hình dạng JSON khác hẳn nhau, nên có hàm bóc riêng cho nền tảng nào
lệch chuẩn (YouTube, Instagram, Kuaishou); còn lại dùng bộ bóc chung theo tên trường.
"""

import logging
import requests
from typing import Optional
from django.conf import settings

from .tikhub_cache import goi_co_dem

logger = logging.getLogger(__name__)

# Giu ngan hon thoi gian cho cua BE (30s) de BE con kip bao loi tu te thay vi bi cat ngang.
TIMEOUT = 25

# Đã đối chiếu openapi.json của TikHub + thử thật từng cái.
#   arg='id'  → truyền mã video      arg='url' → endpoint chỉ nhận link
PLATFORM_ENDPOINTS = {
    'douyin':      {'path': '/api/v1/douyin/web/fetch_one_video',               'param': 'aweme_id',    'arg': 'id'},
    'tiktok':      {'path': '/api/v1/tiktok/app/v3/fetch_one_video',            'param': 'aweme_id',    'arg': 'id'},
    # get_video_info (v1) trả 400 kể cả với tham số mẫu trong tài liệu; v2 chạy ổn.
    'youtube':     {'path': '/api/v1/youtube/web/get_video_info_v2',            'param': 'video_id',    'arg': 'id'},
    'bilibili':    {'path': '/api/v1/bilibili/web/fetch_one_video',             'param': 'bv_id',       'arg': 'id'},
    'xiaohongshu': {'path': '/api/v1/xiaohongshu/app_v2/get_video_note_detail', 'param': 'note_id',     'arg': 'id'},
    'kuaishou':    {'path': '/api/v1/kuaishou/web/fetch_one_video',             'param': 'share_text',  'arg': 'url'},
    # fetch_post_info (v2) trả 400 với shortcode; v1 fetch_post_by_url chạy ổn.
    'instagram':   {'path': '/api/v1/instagram/v1/fetch_post_by_url',           'param': 'post_url',    'arg': 'url'},
}

SUPPORTED_PLATFORMS = sorted(PLATFORM_ENDPOINTS.keys())


def _tikhub_base() -> str:
    return getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')


def _to_int(value) -> int:
    """Số về int an toàn: TikHub trả lúc int, lúc chuỗi ("1600000000"), lúc None, lúc -1."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().replace(',', '')
    if not text:
        return 0
    try:
        return max(0, int(float(text)))
    except (TypeError, ValueError):
        return 0


def _first_url(value) -> str:
    """Ảnh bìa: chỗ trả chuỗi, chỗ trả {'url_list': [...]}, chỗ trả mảng chuỗi/mảng dict."""
    if isinstance(value, str):
        return value if value.startswith('http') else ''
    if isinstance(value, list):
        for item in value:
            got = _first_url(item)
            if got:
                return got
        return ''
    if isinstance(value, dict):
        for key in ('url_list', 'urlList', 'urls', 'thumbnails', 'url', 'src'):
            got = _first_url(value.get(key))
            if got:
                return got
    return ''


def _dig(data, *keys):
    """Giá trị đầu tiên khác rỗng trong nhiều khoá ứng viên."""
    if not isinstance(data, dict):
        return None
    for key in keys:
        if data.get(key) not in (None, ''):
            return data[key]
    return None


def _count_of(value):
    """Instagram gói số đếm trong {'count': n}."""
    if isinstance(value, dict):
        return value.get('count')
    return value


# ─────────────────────────── bóc riêng cho từng nền tảng ───────────────────────────

def _shape_youtube(data: dict) -> dict:
    """YouTube trả nguyên player response; dữ liệu nằm ở videoDetails.
    Lưu ý: endpoint này KHÔNG có số tim/bình luận (player response của YouTube không mang)."""
    vd = data.get('videoDetails') or {}
    return {
        'title': vd.get('title') or '',
        'description': vd.get('shortDescription') or '',
        'author_name': vd.get('author') or '',
        'author_username': vd.get('channelId') or '',
        'thumbnail_url': _first_url(vd.get('thumbnail')),
        'views_count': _to_int(vd.get('viewCount')),
        'likes_count': 0,
        'comments_count': 0,
        'shares_count': 0,
    }


def _shape_instagram(data: dict) -> dict:
    """Instagram trả cấu trúc GraphQL: số đếm nằm trong edge_*.count, tiêu đề trong edges[0].node.text."""
    owner = data.get('owner') or {}
    caption = ''
    edges = ((data.get('edge_media_to_caption') or {}).get('edges') or [])
    if edges and isinstance(edges[0], dict):
        caption = ((edges[0].get('node') or {}).get('text')) or ''
    likes = _count_of(_dig(data, 'edge_media_preview_like', 'edge_liked_by'))
    comments = _count_of(_dig(data, 'edge_media_preview_comment', 'edge_media_to_parent_comment'))
    return {
        'title': caption,
        'description': data.get('accessibility_caption') or '',
        'author_name': owner.get('full_name') or owner.get('username') or '',
        'author_username': owner.get('username') or '',
        'thumbnail_url': _first_url(_dig(data, 'display_url', 'thumbnail_src')),
        'views_count': _to_int(_dig(data, 'video_view_count', 'video_play_count')),
        'likes_count': _to_int(likes),
        'comments_count': _to_int(comments),
        'shares_count': 0,   # Instagram không công khai số chia sẻ
    }


def _shape_kuaishou(data: dict) -> dict:
    """Kuaishou để mọi thứ phẳng ở cấp cao nhất, tác giả không phải object riêng."""
    return {
        'title': data.get('caption') or '',
        'description': '',
        'author_name': data.get('userName') or '',
        'author_username': data.get('kwaiId') or str(data.get('userId') or ''),
        'thumbnail_url': _first_url(_dig(data, 'coverUrls', 'webpCoverUrls')),
        'views_count': _to_int(data.get('viewCount')),
        'likes_count': _to_int(data.get('likeCount')),
        'comments_count': _to_int(data.get('commentCount')),
        'shares_count': _to_int(_dig(data, 'shareCount', 'forwardCount')),
    }


PLATFORM_SHAPERS = {
    'youtube': _shape_youtube,
    'instagram': _shape_instagram,
    'kuaishou': _shape_kuaishou,
}


def _shape_generic(node: dict) -> dict:
    """Douyin / TikTok / Bilibili / Xiaohongshu — bóc theo tên trường."""
    stats = node.get('statistics') or node.get('stats') or node.get('stat') \
        or node.get('interact_info') or node.get('metrics') or {}
    if not isinstance(stats, dict):
        stats = {}
    # Xiaohongshu để số liệu phẳng ngay trên note (liked_count, comments_count, shared_count).
    pool = {**node, **stats} if not stats else stats
    if not stats:
        pool = node

    author = node.get('author') or node.get('owner') or node.get('user') or {}
    if not isinstance(author, dict):
        author = {}

    return {
        'title': _dig(node, 'desc', 'title', 'caption', 'display_title', 'content') or '',
        'description': _dig(node, 'description') or '',
        'author_name': _dig(author, 'nickname', 'name', 'uname', 'nick_name', 'full_name') or '',
        'author_username': _dig(author, 'unique_id', 'uniqueId', 'user_id', 'red_id', 'mid', 'username') or '',
        'thumbnail_url': (
            _first_url(_dig(node, 'cover', 'origin_cover', 'thumbnail', 'pic', 'preview_image'))
            or _first_url((node.get('video') or {}).get('cover') if isinstance(node.get('video'), dict) else None)
            or _first_url(node.get('image_list'))
        ),
        'views_count': _to_int(_dig(pool, 'play_count', 'playCount', 'view_count', 'viewCount', 'view', 'views')),
        'likes_count': _to_int(_dig(pool, 'digg_count', 'diggCount', 'like_count', 'likeCount',
                                    'liked_count', 'likedCount', 'like')),
        'comments_count': _to_int(_dig(pool, 'comment_count', 'commentCount', 'comments_count', 'reply', 'reply_count')),
        'shares_count': _to_int(_dig(pool, 'share_count', 'shareCount', 'shared_count', 'share', 'forward_count')),
    }


def _unwrap(body: dict) -> dict:
    """Bóc lớp vỏ khác nhau giữa các nền tảng để lấy object mô tả video."""
    data = body.get('data') if isinstance(body, dict) else None
    # Kuaishou trả thẳng một mảng.
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    if not isinstance(data, dict):
        return {}
    for key in ('aweme_detail', 'aweme_details', 'video_detail', 'note_detail',
                'data', 'item', 'items', 'result', 'post'):
        inner = data.get(key)
        if isinstance(inner, dict):
            # Xiaohongshu lồng 2 lớp: data.data là mảng note.
            deeper = inner.get('data')
            if isinstance(deeper, list) and deeper and isinstance(deeper[0], dict):
                return deeper[0]
            return inner
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            return inner[0]
    return data


def _clip(shaped: dict, platform: str) -> dict:
    return {
        'platform': platform,
        'title': str(shaped.get('title') or '')[:500],
        'description': str(shaped.get('description') or '')[:2000],
        'author_name': str(shaped.get('author_name') or '')[:255],
        'author_username': str(shaped.get('author_username') or '')[:255],
        'thumbnail_url': str(shaped.get('thumbnail_url') or '')[:1900],
        'views_count': _to_int(shaped.get('views_count')),
        'likes_count': _to_int(shaped.get('likes_count')),
        'comments_count': _to_int(shaped.get('comments_count')),
        'shares_count': _to_int(shaped.get('shares_count')),
    }


def fetch_video_detail(platform: str, video_id: str = '', video_url: str = '') -> Optional[dict]:
    """
    Trả dict đã chuẩn hoá, hoặc None nếu không lấy được.

    KHÔNG ném lỗi ra ngoài: đây là bước làm giàu dữ liệu, hỏng thì đề xuất vẫn phải đi tiếp
    (chỉ là thiếu số liệu), không được chặn người dùng.
    """
    platform = (platform or '').lower().strip()
    config = PLATFORM_ENDPOINTS.get(platform)
    if not config:
        logger.info(f"[VIDEO-DETAIL] Nen tang khong ho tro: {platform}")
        return None

    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        logger.warning("[VIDEO-DETAIL] TIKHUB_API_KEY chua cau hinh")
        return None

    arg_value = video_url if config['arg'] == 'url' else video_id
    if not arg_value:
        need = 'link video' if config['arg'] == 'url' else 'ma video'
        logger.info(f"[VIDEO-DETAIL] {platform}: thieu {need}")
        return None

    # Dùng CHUNG bộ đệm với tikhub_play_url.py — cả hai gọi đúng cùng một endpoint cho
    # douyin/tiktok/kuaishou/xiaohongshu, gọi riêng là trả tiền hai lần. Xem tikhub_cache.py.
    resp = goi_co_dem(
        _tikhub_base(), config['path'], {config['param']: arg_value}, api_key, timeout=TIMEOUT
    )
    if resp is None:
        return None

    if resp.status_code != 200:
        logger.error(f"[VIDEO-DETAIL] {platform} HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    try:
        body = resp.json()
    except ValueError:
        logger.error(f"[VIDEO-DETAIL] {platform} tra ve khong phai JSON")
        return None

    if body.get('code') != 200:
        logger.error(f"[VIDEO-DETAIL] {platform} API code={body.get('code')} msg={str(body.get('message'))[:120]}")
        return None

    node = _unwrap(body)
    if not node:
        logger.error(f"[VIDEO-DETAIL] {platform} khong co du lieu video")
        return None

    shaper = PLATFORM_SHAPERS.get(platform)
    # YouTube/Instagram cần cả cây data gốc, không phải node đã bóc.
    raw = body.get('data') if isinstance(body.get('data'), dict) else node
    shaped = shaper(raw if platform in ('youtube', 'instagram') else node) if shaper else _shape_generic(node)
    result = _clip(shaped, platform)

    logger.info(
        f"[VIDEO-DETAIL] {platform} id={video_id or arg_value[:40]} "
        f"view={result['views_count']} like={result['likes_count']} cmt={result['comments_count']}"
    )
    return result
