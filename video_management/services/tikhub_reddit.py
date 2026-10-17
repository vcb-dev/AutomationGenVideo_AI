"""TikHub Reddit API — tìm bài theo từ khoá và theo cộng đồng (subreddit), fetch + parse only.

BE (Prisma) sở hữu việc lưu scraper_reddit_subreddits / scraper_reddit_posts; AI chỉ gọi TikHub
và trả dict chuẩn hoá.

Kiểm chứng thật ngày 2026-10-01:
- reddit.com/*.json (không đăng nhập) trả 403 kể cả từ máy local → phải đi qua TikHub.
- app/fetch_dynamic_search: $0.001/lượt, ~7 bài/trang, phân trang bằng pageInfo.endCursor.
  need_format=false mới có nội dung bài (post.content.markdown); need_format=true bỏ mất.
- Lọc theo cộng đồng bằng chính cú pháp tìm kiếm của Reddit: "subreddit:jewelry" trả 7/7 bài
  của r/jewelry — cùng một parser cho cả hai cách cào. (app/fetch_subreddit_feed trả cấu trúc ô
  giao diện "cells", 4 bài/trang, không có nội dung bài.)
"""

import logging
import math
import re
import time
from datetime import datetime, timezone as tz
from typing import Any, Dict, List, Optional, Tuple

import requests
from django.conf import settings

from .tikhub_errors import raise_if_auth_error

logger = logging.getLogger(__name__)

# Giá trị Reddit chấp nhận cho tham số sort / time_range của fetch_dynamic_search.
SEARCH_SORTS = ('RELEVANCE', 'HOT', 'TOP', 'NEW', 'COMMENTS')
TIME_RANGES = ('all', 'year', 'month', 'week', 'day', 'hour')

# Tên subreddit hợp lệ trên Reddit: 2–21 ký tự chữ, số, gạch dưới.
SUBREDDIT_NAME_RE = re.compile(r'^[A-Za-z0-9_]{2,21}$')

# Mỗi trang ~7 bài; chặn số trang để một trang lỗi trả rỗng không thành vòng lặp tốn tiền.
POSTS_PER_PAGE_ESTIMATE = 5
MAX_ATTEMPTS = 3


class RedditConfigError(Exception):
    """Thiếu biến cấu hình — view trả 400 kèm tên biến."""


def _required_setting(name: str) -> str:
    value = str(getattr(settings, name, '') or '').strip()
    if not value:
        raise RedditConfigError(f'Thiếu biến môi trường {name}')
    return value


def _get(path: str, params: Dict[str, Any], source: str) -> Optional[dict]:
    """GET TikHub có thử lại lỗi tạm thời; 401/403/429 ném TikHubAuthError (không thử lại)."""
    base = _required_setting('TIKHUB_API_BASE_URL').rstrip('/')
    headers = {'Authorization': f'Bearer {_required_setting("TIKHUB_API_KEY")}'}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(f'{base}{path}', params=params, headers=headers, timeout=30)
        except requests.RequestException as e:
            logger.warning(f'[REDDIT-TIKHUB] {source} attempt {attempt} network error: {e}')
            if attempt < MAX_ATTEMPTS:
                time.sleep(1.5)
                continue
            return None

        if not resp.ok:
            raise_if_auth_error(resp, f'reddit {source}')
            logger.warning(f'[REDDIT-TIKHUB] {source} attempt {attempt} HTTP {resp.status_code}: {resp.text[:300]}')
            if attempt < MAX_ATTEMPTS:
                time.sleep(1.5)
                continue
            return None

        body = resp.json() or {}
        return body.get('data') or {}
    return None


def _search_page(query: str, sort: str, time_range: str, after: str) -> Tuple[List[dict], str, bool]:
    params = {
        'query': query,
        'search_type': 'post',
        'sort': sort,
        'time_range': time_range,
        'need_format': 'false',
        'allow_nsfw': 0,
    }
    if after:
        params['after'] = after
    data = _get('/api/v1/reddit/app/fetch_dynamic_search', params, f'search "{query}"')
    main = (((data or {}).get('search') or {}).get('dynamic') or {}).get('components', {}).get('main') or {}

    posts: List[dict] = []
    for edge in main.get('edges') or []:
        for child in ((edge or {}).get('node') or {}).get('children') or []:
            post = (child or {}).get('post')
            if isinstance(post, dict):
                posts.append(post)

    page_info = main.get('pageInfo') or {}
    return posts, str(page_info.get('endCursor') or ''), bool(page_info.get('hasNextPage'))


def search_posts(query: str, sort: str, time_range: str, count: int) -> List[dict]:
    """Bài Reddit thô theo câu tìm kiếm, phân trang tới khi đủ `count` hoặc hết trang."""
    collected: List[dict] = []
    seen = set()
    after = ''
    max_pages = math.ceil(count / POSTS_PER_PAGE_ESTIMATE) + 1
    for _ in range(max_pages):
        posts, after, has_next = _search_page(query, sort, time_range, after)
        new = [p for p in posts if p.get('id') and p['id'] not in seen]
        for p in new:
            seen.add(p['id'])
        collected.extend(new)
        if len(collected) >= count or not has_next or not after or not new:
            break
    result = collected[:count]
    logger.info(f'[REDDIT-TIKHUB] search "{query}" sort={sort} time={time_range}: {len(result)} bài (yêu cầu {count})')
    return result


def normalize_subreddit_name(raw: str) -> str:
    """'r/Jewelry', '/r/jewelry/', 'https://www.reddit.com/r/jewelry/top/?t=month' → 'jewelry'.

    Trả '' nếu không ra được tên hợp lệ. Tên subreddit không phân biệt hoa thường trên Reddit.
    """
    text = (raw or '').strip()
    m = re.search(r'(?:^|/)r/([A-Za-z0-9_]+)', text)
    name = m.group(1) if m else text.lstrip('/')
    return name.lower() if SUBREDDIT_NAME_RE.match(name) else ''


def fetch_subreddit_info(name: str) -> Optional[dict]:
    """Thông tin cộng đồng — chỉ gọi khi tìm không ra bài nào, để phân biệt "không có bài" với "không tồn tại"."""
    data = _get('/api/v1/reddit/app/fetch_subreddit_info',
                {'subreddit_name': name, 'need_format': 'true'}, f'subreddit_info r/{name}')
    info = (data or {}).get('subredditInfoByName')
    return info if isinstance(info, dict) and info.get('name') else None


def parse_subreddit(info: Optional[dict]) -> Optional[dict]:
    """Thông tin subreddit chuẩn hoá — nguồn là post.subreddit hoặc subredditInfoByName (cùng khoá)."""
    if not isinstance(info, dict) or not info.get('name'):
        return None
    styles = info.get('styles') or {}
    description = info.get('publicDescriptionText') or ''
    return {
        'name': str(info['name']).lower(),
        'display_name': info.get('prefixedName') or f"r/{info['name']}",
        'title': info.get('title') or '',
        'description': description,
        'icon_url': styles.get('icon') or '',
        'subscribers_count': int(info.get('subscribersCount') or 0),
        'is_nsfw': bool(info.get('isNsfw')),
    }


# Ảnh hiện trên thẻ bài: bản rộng 640px là đủ nét. Bản gốc ("source"/"url") có khi 3024px —
# quá nặng cho một thẻ nhỏ (đo thật 2026-10-01), chỉ dùng khi Reddit không có bản thu nhỏ.
_IMAGE_SIZES = ('xlarge', 'xxlarge', 'large', 'xxxlarge', 'medium', 'source', 'url')


def _pick_image(sizes: Optional[dict]) -> str:
    if not isinstance(sizes, dict):
        return ''
    for key in _IMAGE_SIZES:
        value = sizes.get(key)
        url = value.get('url') if isinstance(value, dict) else (value if key == 'url' else None)
        if url:
            return str(url)
    return ''


def _media(post: dict) -> Tuple[str, str, str]:
    """(media_type, thumbnail_url, video_url)."""
    media = post.get('media') or {}
    video_url = str((media.get('streaming') or {}).get('hlsUrl') or '')
    thumbnail = _pick_image(media.get('still')) or ((post.get('thumbnail') or {}).get('url') or '')
    gallery = (post.get('gallery') or {}).get('items') or []
    hint = str(post.get('postHint') or '').upper()

    if video_url or hint in ('HOSTED_VIDEO', 'RICH_VIDEO'):
        return 'VIDEO', thumbnail, video_url
    if gallery:
        # post.thumbnail của album chỉ là ô vuông 140px — ưu tiên ảnh đầu tiên của album.
        return 'GALLERY', _pick_image((gallery[0] or {}).get('media')) or thumbnail, ''
    if hint == 'IMAGE' or (thumbnail and not post.get('isSelfPost')):
        return 'IMAGE', thumbnail, ''
    if hint == 'LINK':
        return 'LINK', thumbnail, ''
    return 'TEXT', '', ''


def parse_reddit_posts(raw_posts: List[dict], keyword: str = '') -> List[dict]:
    """Bài Reddit thô (SubredditPost của TikHub) → dict chuẩn cho BE lưu."""
    parsed = []
    seen = set()
    for post in raw_posts:
        if not isinstance(post, dict):
            continue
        raw_id = str(post.get('id') or '')
        post_id = raw_id[3:] if raw_id.startswith('t3_') else raw_id
        title = str(post.get('postTitle') or '')
        if not post_id or post_id in seen or not title:
            continue
        seen.add(post_id)

        content = post.get('content') or {}
        text = str(content.get('markdown') or '') if isinstance(content, dict) else ''
        permalink = str(post.get('permalink') or '')
        author = post.get('authorInfo') or {}
        subreddit = parse_subreddit(post.get('subreddit')) or {}
        media_type, thumbnail_url, video_url = _media(post)
        outbound = str((post.get('outboundLink') or {}).get('url') or post.get('url') or '')

        created = str(post.get('createdAt') or '')
        try:
            date_posted = datetime.strptime(created, '%Y-%m-%dT%H:%M:%S.%f%z').isoformat()
        except ValueError:
            date_posted = datetime.now(tz.utc).isoformat()

        parsed.append({
            'post_id': post_id,
            'title': title,
            'text': text,
            # Link bài trên Reddit (dạng chuẩn công khai của Reddit, không phải cấu hình).
            'url': f'https://www.reddit.com{permalink}' if permalink.startswith('/') else permalink,
            'link_url': '' if post.get('isSelfPost') else outbound,
            'subreddit_name': subreddit.get('name', ''),
            'subreddit': subreddit or None,
            'author': str(author.get('name') or ''),
            'author_avatar': str(((author.get('iconSmall') or {}).get('url')) or ''),
            'media_type': media_type,
            'thumbnail_url': thumbnail_url,
            'video_url': video_url,
            'score': int(post.get('score') or 0),
            'comments_count': int(post.get('commentCount') or 0),
            'upvote_ratio': post.get('upvoteRatio'),
            'is_nsfw': bool(post.get('isNsfw')),
            'date_posted': date_posted,
            'search_keyword': keyword,
        })
    return parsed
