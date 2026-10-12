"""TikHub Threads Web API client + parser (fetch-only, không đụng DB).

Flow: username → TikHub fetch_user_info → fetch_user_posts (pagination) → parse thành dict.
BE (Prisma) là nơi lưu vào scraper_threads_profiles / scraper_threads_posts.
"""

import logging
import time
import requests
import json
import re
import urllib.parse
from datetime import datetime, timezone as tz
from typing import Optional, List, Dict, Any, Tuple
from django.conf import settings

from .tikhub_errors import raise_if_auth_error

logger = logging.getLogger(__name__)


def _tikhub_base() -> str:
    return getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')


def fetch_user_info(username: str) -> Optional[dict]:
    """Fetch Threads user info qua TikHub (hỗ trợ retry khi gặp lỗi tạm thời).

    Returns raw `data` dict từ TikHub, hoặc None nếu lỗi.
    """
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")

    headers = {'Authorization': f'Bearer {api_key}'}
    for attempt in range(1, 4):
        try:
            resp = requests.get(
                f'{_tikhub_base()}/api/v1/threads/web/fetch_user_info',
                params={'username': username},
                headers=headers,
                timeout=30,
            )
        except Exception as e:
            logger.error(f'[THREADS-TIKHUB] fetch_user_info @{username} attempt {attempt} network error: {e}')
            if attempt < 3:
                time.sleep(1.5)
                continue
            return None

        if not resp.ok:
            raise_if_auth_error(resp, 'threads fetch_user_info')
            logger.warning(f'[THREADS-TIKHUB] fetch_user_info @{username} attempt {attempt} HTTP {resp.status_code}: {resp.text[:300]}')
            if attempt < 3:
                time.sleep(1.5)
                continue
            return None

        body = resp.json()
        data = body.get('data') or {}
        user_obj = data.get('user') if isinstance(data.get('user'), dict) else data
        if user_obj:
            pk = user_obj.get('pk') or user_obj.get('id')
            logger.info(f'[THREADS-TIKHUB] fetch_user_info @{username}: ok (pk={pk})')
            return data

        if attempt < 3:
            time.sleep(1.5)

    return None


def parse_threads_user_info(username: str, info: dict) -> dict:
    """Parse kết quả fetch_user_info thành dict thông tin profile chuẩn hóa cho BE."""
    if not info:
        return {
            'threads_user_id': '',
            'username': username,
            'name': username,
            'url': f'https://www.threads.net/@{username}',
            'avatar_url': '',
            'biography': '',
            'followers_count': 0,
            'is_verified': False,
        }
    user_obj = info.get('user') if isinstance(info.get('user'), dict) else info
    pk = str(user_obj.get('pk') or user_obj.get('id') or '')
    uname = user_obj.get('username') or username
    return {
        'threads_user_id': pk,
        'username': uname,
        'name': user_obj.get('full_name') or uname,
        'url': f'https://www.threads.net/@{uname}',
        'avatar_url': user_obj.get('profile_pic_url') or '',
        'biography': user_obj.get('biography') or '',
        'followers_count': int(user_obj.get('follower_count') or 0),
        'is_verified': bool(user_obj.get('is_verified', False)),
    }


def fetch_threads_posts(user_id: str, count: int = 50) -> list:
    """Fetch danh sách bài viết Threads của user qua TikHub (hỗ trợ phân trang và retry).

    Args:
        user_id: ID người dùng Threads (lấy từ pk của fetch_user_info)
        count: Số lượng bài viết tối đa cần lấy
    Returns:
        List of raw thread items từ TikHub
    """
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")

    headers = {'Authorization': f'Bearer {api_key}'}
    items: List[Dict[str, Any]] = []
    end_cursor: Optional[str] = None
    max_pages = 6

    while len(items) < count and max_pages > 0:
        max_pages -= 1
        params: Dict[str, Any] = {'user_id': user_id}
        if end_cursor:
            params['end_cursor'] = end_cursor

        page_items = []
        for attempt in range(1, 4):
            try:
                resp = requests.get(
                    f'{_tikhub_base()}/api/v1/threads/web/fetch_user_posts',
                    params=params,
                    headers=headers,
                    timeout=30,
                )
            except Exception as e:
                logger.error(f'[THREADS-TIKHUB] fetch_user_posts user {user_id} attempt {attempt} network error: {e}')
                if attempt < 3:
                    time.sleep(1.5)
                    continue
                break

            if not resp.ok:
                raise_if_auth_error(resp, 'threads fetch_user_posts')
                logger.warning(f'[THREADS-TIKHUB] fetch_user_posts user {user_id} attempt {attempt} HTTP {resp.status_code}: {resp.text[:300]}')
                if attempt < 3:
                    time.sleep(1.5)
                    continue
                break

            body = resp.json()
            data = body.get('data') or {}
            media_data = data.get('mediaData') if isinstance(data.get('mediaData'), dict) else {}
            edges = media_data.get('edges') or []
            if edges and isinstance(edges, list):
                page_items = [edge.get('node') for edge in edges if isinstance(edge, dict) and edge.get('node')]
            else:
                page_items = data.get('threads') or data.get('items') or []

            end_cursor = data.get('next_cursor') or data.get('end_cursor')
            has_more = data.get('has_more')
            if not end_cursor or has_more is False:
                end_cursor = None
            break

        if not page_items:
            break

        items.extend(page_items)
        if not end_cursor:
            break

    logger.info(f'[THREADS-TIKHUB] user_id {user_id}: fetched {len(items)} posts')
    return items[:count]


def search_threads_web(query: str, count: int = 50) -> list:
    """Tìm kiếm bài viết Threads công khai bằng cách crawl trang kết quả tìm kiếm Threads.
    
    Hỗ trợ lấy bài theo từ khoá/chủ đề (ví dụ: 'topic', 'vàng bạc', 'công sở', 'review').
    Duyệt qua các chế độ tìm kiếm của Threads (default, recent, tags) để đảm bảo gom đủ
    số lượng bài viết yêu cầu mà không bị thiếu hụt.
    """
    import re
    import urllib.parse

    encoded = urllib.parse.quote_plus(query.strip())
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Dest': 'document',
    }

    thread_items = []
    seen_ids = set()

    def find_items(obj, collector):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == 'thread_items' and isinstance(v, list):
                    collector.extend(v)
                else:
                    find_items(v, collector)
        elif isinstance(obj, list):
            for elem in obj:
                find_items(elem, collector)

    # Duyệt qua các chế độ tìm kiếm của Threads (default -> recent -> tags)
    serp_types = ['default']
    if count > 15:
        serp_types.extend(['recent', 'tags'])

    for serp in serp_types:
        url = f'https://www.threads.net/search?q={encoded}&serp_type={serp}'
        for attempt in range(1, 3):
            try:
                resp = requests.get(url, headers=headers, timeout=15)
                if not resp.ok:
                    logger.warning(f'[THREADS-WEB-SEARCH] "{query}" ({serp}) attempt {attempt} HTTP {resp.status_code}')
                    if attempt < 2:
                        time.sleep(0.8)
                        continue
                    break

                scripts = re.findall(r'<script[^>]*type=[\"\']application/json[\"\'][^>]*>(.*?)</script>', resp.text, re.DOTALL)
                page_items = []
                for s in scripts:
                    if 'thread_items' in s:
                        try:
                            data = json.loads(s)
                            find_items(data, page_items)
                        except Exception:
                            pass

                # Khử trùng lặp theo ID bài viết
                for it in page_items:
                    post_obj = it
                    if isinstance(it, dict) and 'thread_items' in it and it['thread_items']:
                        post_obj = it['thread_items'][0].get('post') or it['thread_items'][0]
                    elif isinstance(it, dict) and 'post' in it:
                        post_obj = it['post']
                    pid = str(post_obj.get('id') or post_obj.get('pk') or '') if isinstance(post_obj, dict) else ''
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        thread_items.append(it)
                    elif not pid:
                        thread_items.append(it)

                if len(thread_items) >= count * 2:
                    break
            except Exception as e:
                logger.error(f'[THREADS-WEB-SEARCH] "{query}" ({serp}) attempt {attempt} error: {e}')
                if attempt < 2:
                    time.sleep(0.8)
                    continue

        if len(thread_items) >= count * 2:
            break

    logger.info(f'[THREADS-WEB-SEARCH] query "{query}": found {len(thread_items)} raw items (target: {count})')
    return thread_items[:max(count * 2, 60)]


def search_top_threads(query: str, count: int = 50) -> list:
    """Tìm kiếm nội dung Threads:
    - Nếu query là username hoặc profile URL: thử fetch profile qua TikHub.
    - Nếu query là từ khoá hoặc TikHub không có kết quả: cào trực tiếp qua search_threads_web.
    """
    import re

    clean = query.strip().replace('https://www.threads.net/', '').replace('threads.net/', '').lstrip('@').split('?')[0].split('/')[0]
    if clean and ' ' not in clean and re.match(r'^[a-zA-Z0-9._]+$', clean):
        try:
            info = fetch_user_info(clean)
            if info:
                user_obj = info.get('user') if isinstance(info.get('user'), dict) else info
                pk = user_obj.get('pk') or user_obj.get('id')
                if pk:
                    posts = fetch_threads_posts(str(pk), count=count)
                    if posts:
                        return posts
        except Exception as e:
            logger.warning(f'[THREADS-SEARCH] fetch user @{clean} failed: {e}')

    return search_threads_web(query, count=count)


def _parse_datetime(val) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=tz.utc)
    try:
        return datetime.fromisoformat(str(val).replace('Z', '+00:00'))
    except Exception:
        return None


VI_REGEX = re.compile(r'[àáảãạăắằẳẵặâấầẩẫậđèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵ]', re.IGNORECASE)
VI_COMMON_WORDS = {
    'thì', 'là', 'mà', 'của', 'người', 'không', 'được', 'trong', 'với', 'cho', 'những',
    'dạo', 'thấy', 'bảo', 'có', 'mình', 'bạn', 'anh', 'em', 'chị', 'xem', 'hay', 'lại',
    'làm', 'còn', 'nên', 'cũng', 'quá', 'rất', 'luôn', 'nhiều', 'thế', 'nào', 'gì', 'ơi',
    'nhé', 'nha', 'chứ', 'đâu', 'đây', 'đó', 'này', 'kia', 'ở', 'đang', 'ra', 'vào', 'đi'
}


def is_vietnamese_text(text: str) -> bool:
    """Nhận diện nội dung có phải tiếng Việt hay không (dựa trên dấu thanh hoặc từ vựng phổ biến)."""
    if not text:
        return False
    if VI_REGEX.search(text):
        return True
    words = set(re.findall(r'\b\w+\b', text.lower()))
    return len(words & VI_COMMON_WORDS) >= 2


def parse_threads_posts(items: list, default_username: str = '', query: str = '') -> list:
    """Parse raw TikHub thread items thành danh sách post chuẩn hóa cho BE."""
    parsed = []
    seen_ids = set()

    for raw in items:
        item = raw
        if isinstance(raw, dict) and 'thread_items' in raw and raw['thread_items']:
            item = raw['thread_items'][0].get('post') or raw['thread_items'][0]
        elif isinstance(raw, dict) and 'post' in raw:
            item = raw['post']

        if not isinstance(item, dict):
            continue

        post_id = str(item.get('id') or item.get('pk') or '').strip()
        if not post_id or post_id in seen_ids:
            continue
        seen_ids.add(post_id)

        code = item.get('code') or ''
        caption_obj = item.get('caption') or {}
        text = ''
        if isinstance(caption_obj, dict):
            text = caption_obj.get('text') or ''
        elif isinstance(caption_obj, str):
            text = caption_obj
        if not text and item.get('text'):
            text = str(item.get('text'))

        user_obj = item.get('user') or {}
        author_username = user_obj.get('username') or default_username
        author_name = user_obj.get('full_name') or author_username
        author_avatar = user_obj.get('profile_pic_url') or ''

        # Media detection: Video hoặc Image hoặc Text
        # ĐẶC BIỆT QUAN TRỌNG: Phải khởi tạo rỗng mỗi vòng lặp để không bị dính thumbnail của bài trước!
        media_type = 'TEXT'
        thumbnail_url = ''
        video_url = ''

        video_versions = item.get('video_versions') or []
        image_versions2 = item.get('image_versions2') or {}
        carousel_media = item.get('carousel_media') or []

        # Lấy ảnh thumbnail
        candidates = image_versions2.get('candidates') or [] if isinstance(image_versions2, dict) else []
        if candidates and isinstance(candidates, list) and len(candidates) > 0:
            thumbnail_url = candidates[0].get('url') or ''
        elif carousel_media and isinstance(carousel_media, list) and len(carousel_media) > 0 and carousel_media[0].get('image_versions2'):
            cands = carousel_media[0]['image_versions2'].get('candidates') or []
            if cands and len(cands) > 0:
                thumbnail_url = cands[0].get('url') or ''

        if video_versions and isinstance(video_versions, list) and len(video_versions) > 0:
            media_type = 'VIDEO'
            video_url = video_versions[0].get('url') or ''
        elif carousel_media and isinstance(carousel_media, list) and len(carousel_media) > 0:
            media_type = 'CAROUSEL'
        elif thumbnail_url:
            media_type = 'IMAGE'
        else:
            media_type = 'TEXT'

        # Lấy chỉ số tương tác
        likes_count = int(item.get('like_count') or 0)
        
        text_app_info = item.get('text_post_app_info') or {}
        replies_count = 0
        reposts_count = 0
        quotes_count = 0
        if isinstance(text_app_info, dict):
            replies_count = int(text_app_info.get('direct_reply_count') or text_app_info.get('reply_count') or 0)
            reposts_count = int(text_app_info.get('repost_count') or 0)
            quotes_count = int(text_app_info.get('quote_count') or 0)
        if replies_count == 0:
            replies_count = int(item.get('reply_count') or 0)

        views_count = int(item.get('view_count') or item.get('play_count') or 0)

        # Ngày đăng
        taken_at = item.get('taken_at')
        dt = _parse_datetime(taken_at)
        date_posted_str = dt.isoformat() if dt else datetime.now(tz.utc).isoformat()

        post_url = f'https://www.threads.net/@{author_username}/post/{code}' if code and author_username else f'https://www.threads.net/t/{code or post_id}'

        is_vn = is_vietnamese_text(text)

        parsed.append({
            'post_id': post_id,
            'shortcode': code,
            'url': post_url,
            'text': text,
            'media_type': media_type,
            'thumbnail_url': thumbnail_url,
            'video_url': video_url,
            'views_count': views_count,
            'likes_count': likes_count,
            'replies_count': replies_count,
            'reposts_count': reposts_count,
            'quotes_count': quotes_count,
            'date_posted': date_posted_str,
            'author_username': author_username,
            'author_name': author_name,
            'author_avatar': author_avatar,
            'is_vietnamese': is_vn,
        })

    # Nếu có query tìm kiếm:
    # 1. Chuẩn hoá từ khoá (loại bỏ ký tự đặc biệt ở đầu/cuối như : # @).
    # 2. LỌC CHẶT CHẼ: Chỉ giữ lại các bài viết thực sự chứa từ khoá (loại bỏ bài rác/không liên quan).
    # 3. Ưu tiên bài viết TIẾNG VIỆT lên hàng đầu, sau đó sắp xếp theo mức tương tác (likes) giảm dần.
    if query:
        import unicodedata
        # Chuẩn hoá từ khoá: 'topic:' -> 'topic', '#thoitrang' -> 'thoitrang'
        clean_q = re.sub(r'^[#@]+|[^\w\s]+$', '', query).strip().lower()
        norm_q = unicodedata.normalize('NFKD', clean_q)
        tokens = [t for t in clean_q.split() if len(t) > 1]
        tokens_norm = [unicodedata.normalize('NFKD', t) for t in tokens]

        matched = []

        for p in parsed:
            p_text = p['text'].lower()
            p_author = p['author_username'].lower()
            p_name = p['author_name'].lower()
            p_text_norm = unicodedata.normalize('NFKD', p_text)
            p_name_norm = unicodedata.normalize('NFKD', p_name)

            # Khớp toàn bộ chuỗi tìm kiếm hoặc khớp username/tác giả
            full_match = (clean_q in p_text or clean_q in p_author or clean_q in p_name
                          or norm_q in p_text_norm or norm_q in p_name_norm)

            # Nếu từ khoá gồm nhiều từ (ví dụ: 'vàng bạc'), kiểm tra khớp tất cả token
            token_match = False
            if not full_match and len(tokens) > 1:
                token_match = all(t in p_text or t_n in p_text_norm for t, t_n in zip(tokens, tokens_norm))

            if full_match or token_match:
                matched.append(p)

        # Xếp hạng: Bài tiếng Việt lên trước, sau đó xếp theo lượt like cao nhất giảm dần
        matched.sort(key=lambda p: (1 if p.get('is_vietnamese') else 0, p['likes_count']), reverse=True)
        parsed = matched
    else:
        # Nếu không có query (cào theo profile): ưu tiên bài tiếng Việt và xếp theo like
        parsed.sort(key=lambda p: (1 if p.get('is_vietnamese') else 0, p['likes_count']), reverse=True)

    return parsed
