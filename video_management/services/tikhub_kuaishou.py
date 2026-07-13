"""TikHub Kuaishou profile + video scraper — fetch + parse only (không đụng DB).

Flow: eid (từ URL profile kuaishou.com/profile/{eid}) → TikHub fetch_one_user_v2
(thông tin profile, RESOLVE ra numeric user_id) → fetch_user_hot_post (danh sách
video, cần numeric user_id vừa resolve) → parse thành dict.
BE (Prisma) là nơi upsert vào scraper_kuaishou_profiles/scraper_kuaishou_videos.

QUAN TRỌNG — 2 không gian ID khác nhau cho cùng 1 tài khoản Kuaishou:
- eid (chuỗi, vd "3xz63mn6fngqtiq"): lấy từ URL profile, dùng cho
  fetch_one_user_v2. fetch_user_hot_post KHÔNG chấp nhận eid.
- user_id (numeric, vd "228905802"): CHỈ fetch_user_hot_post chấp nhận. Resolve
  được từ field data.userProfile.profile.user_id trong response của
  fetch_one_user_v2 (đã verify qua mẫu response thật — 2 field nằm cùng object
  profile, cùng ownerCount khớp với dữ liệu fetch_user_info của cùng tài khoản).

Lưu ý: fetch_user_hot_post trả về "top" video của user (tên endpoint gợi ý là
video nổi bật/hot), chưa xác nhận được liệu phân trang có duyệt hết toàn bộ
video công khai của kênh hay chỉ giới hạn ở một tập con "hot" — cần theo dõi số
lượng thực tế cào được so với videos_count lấy từ fetch_one_user_v2.
Tham số phân trang `pcursor` suy ra từ tên field trong response (giống quy ước
cursor của search_video_v2) — có cơ chế phòng vệ chống lặp vô hạn nếu tên tham
số không đúng.
"""

import logging
import re
from datetime import datetime, timezone as tz
from typing import Optional
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _tikhub_base() -> str:
    return f"{getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')}/api/v1/kuaishou"


def _headers() -> dict:
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        raise ValueError("TIKHUB_API_KEY not configured")
    return {"Authorization": f"Bearer {api_key}"}


def _pick_url(url_list: list) -> str:
    if not url_list:
        return ''
    first = url_list[0]
    return first.get('url', '') if isinstance(first, dict) else ''


def _extract_hashtags(text: str) -> list:
    return [m.lstrip('#') for m in re.findall(r'#\w+', text or '')]


def _extract_author_eid(share_info: str) -> str:
    """share_info dạng 'userId=<eid>&photoId=<photo_id>' — userId ở đây luôn là
    eid của TÁC GIẢ video (link share luôn trỏ về profile người đăng), đáng tin
    cậy hơn soundTrack.user (có thể là chủ sở hữu nhạc nền mượn, không phải tác
    giả video)."""
    m = re.search(r'userId=([\w-]+)', share_info or '')
    return m.group(1) if m else ''


# ─── Fetch ────────────────────────────────────────────────────────────────────

def fetch_one_user_v2(eid: str) -> Optional[dict]:
    """Gọi fetch_one_user_v2 (App API, nhận eid) — trả về userProfile dict thô
    (bao gồm cả numeric user_id ở data.profile.user_id để dùng cho
    fetch_user_hot_post) hoặc None nếu lỗi.

    Đây là API thay thế fetch_user_info (Web API) — cùng tác dụng lấy thông tin
    profile, nhưng fetch_one_user_v2 còn resolve được numeric user_id mà
    fetch_user_info không có.
    """
    try:
        resp = requests.get(
            f'{_tikhub_base()}/app/fetch_one_user_v2',
            params={'user_id': eid},
            headers=_headers(),
            timeout=30,
        )
        if not resp.ok:
            logger.error(f'[KUAISHOU] fetch_one_user_v2 {resp.status_code}: {resp.text[:300]}')
            return None
        body = resp.json()
        if body.get('code') != 200:
            logger.error(f'[KUAISHOU] fetch_one_user_v2 API error: {body.get("message")}')
            return None
        return (body.get('data') or {}).get('userProfile') or None
    except requests.RequestException as e:
        logger.error(f'[KUAISHOU] fetch_one_user_v2 request failed: {e}')
        return None


def search_kuaishou_by_keyword(keyword: str, count: int = 30) -> list:
    """Search video theo keyword qua TikHub search_video_v2 (pcursor pagination)."""
    all_items: list = []
    pcursor: Optional[str] = None
    max_iterations = 20

    logger.info(f"[KUAISHOU] Searching keyword='{keyword}' count={count}")

    for _ in range(max_iterations):
        if len(all_items) >= count:
            break

        params: dict = {'keyword': keyword}
        if pcursor:
            params['pcursor'] = pcursor

        try:
            resp = requests.get(
                f'{_tikhub_base()}/app/search_video_v2',
                params=params,
                headers=_headers(),
                timeout=30,
            )
        except requests.RequestException as e:
            logger.error(f'[KUAISHOU] search_video_v2 request failed: {e}')
            break

        if not resp.ok:
            logger.error(f'[KUAISHOU] search_video_v2 {resp.status_code}: {resp.text[:300]}')
            break

        body = resp.json()
        if body.get('code') != 200:
            logger.error(f'[KUAISHOU] search_video_v2 API error: {body.get("message")}')
            break

        data = body.get('data') or {}
        mix_feeds = data.get('mixFeeds') or []
        page_items = [mf.get('feed') for mf in mix_feeds if mf.get('feed')]
        if not page_items:
            break

        if all_items and page_items[0].get('photo_id') == all_items[0].get('photo_id'):
            break

        all_items.extend(page_items)

        next_pcursor = data.get('pcursor')
        if not next_pcursor or next_pcursor == 'no_more':
            break
        pcursor = next_pcursor

    logger.info(f"[KUAISHOU] Total: {len(all_items)} videos for keyword='{keyword}'")
    return all_items[:count]


def fetch_user_hot_post(user_id: str, count: int = 20) -> list:
    """Fetch video của 1 user qua TikHub (pcursor pagination)."""
    all_videos: list = []
    pcursor: Optional[str] = None
    max_iterations = 20

    for _ in range(max_iterations):
        if len(all_videos) >= count:
            break

        params: dict = {'user_id': user_id}
        if pcursor:
            params['pcursor'] = pcursor

        try:
            resp = requests.get(
                f'{_tikhub_base()}/app/fetch_user_hot_post',
                params=params,
                headers=_headers(),
                timeout=30,
            )
        except requests.RequestException as e:
            logger.error(f'[KUAISHOU] fetch_user_hot_post request failed: {e}')
            break

        if not resp.ok:
            logger.error(f'[KUAISHOU] fetch_user_hot_post {resp.status_code}: {resp.text[:300]}')
            break

        body = resp.json()
        if body.get('code') != 200:
            logger.error(f'[KUAISHOU] fetch_user_hot_post API error: {body.get("message")}')
            break

        data = body.get('data') or {}
        page_feeds = data.get('feeds') or []
        if not page_feeds:
            break

        # Phòng vệ: nếu trang mới trùng trang cũ (pcursor không được TikHub nhận
        # ra) thì dừng luôn, tránh lặp vô hạn / trùng dữ liệu.
        if all_videos and page_feeds[0].get('photo_id') == all_videos[0].get('photo_id'):
            break

        all_videos.extend(page_feeds)

        next_pcursor = data.get('pcursor')
        if not next_pcursor or next_pcursor == 'no_more':
            break
        pcursor = next_pcursor

    return all_videos[:count]


# ─── Parse ────────────────────────────────────────────────────────────────────

def parse_kuaishou_profile(data: dict, eid: str) -> Optional[dict]:
    """Parse response fetch_one_user_v2 thành dict thô (không ghi DB).

    eid là input đã biết trước (không có trong response) — truyền thẳng vào để
    lưu lại. user_id (numeric) mới là giá trị RESOLVE được từ response, dùng
    cho fetch_user_hot_post ở các lần cào sau.
    """
    profile = (data or {}).get('profile') or {}
    owner_count = (data or {}).get('ownerCount') or {}

    user_id = str(profile.get('user_id') or '')
    if not user_id:
        return None

    return {
        'user_id': user_id,
        'eid': eid,
        'username': profile.get('kwaiId') or '',
        'nickname': (profile.get('user_name') or '')[:500],
        'url': f'https://www.kuaishou.com/profile/{eid}',
        'avatar_url': profile.get('headurl') or '',
        'biography': profile.get('user_text') or '',
        'gender': profile.get('user_sex') or '',
        'followers_count': int(owner_count.get('fan') or 0),
        'following_count': int(owner_count.get('follow') or 0),
        # total_photo_like là field tổng lượt thích trong fetch_one_user_v2 —
        # khác tên với ownerCount.like của fetch_user_info (giữ fallback phòng
        # trường hợp field đổi tên).
        'likes_count': int(owner_count.get('total_photo_like') or owner_count.get('like') or 0),
        'videos_count': int(owner_count.get('photo_public') or 0),
    }


def parse_kuaishou_videos(feeds: list) -> list:
    """Parse list feeds thô thành list dict (không ghi DB)."""
    parsed = []
    for f in feeds:
        photo_id = str(f.get('photo_id') or '')
        if not photo_id:
            continue

        caption = f.get('caption') or f.get('pureTitle') or ''
        timestamp = f.get('timestamp') or 0
        date_posted = datetime.fromtimestamp(timestamp / 1000, tz=tz.utc) if timestamp else datetime.now(tz.utc)

        parsed.append({
            'post_id': photo_id,
            'url': f'https://www.kuaishou.com/short-video/{photo_id}',
            'description': caption[:2000],
            'hashtags': _extract_hashtags(caption),
            'thumbnail_url': _pick_url(f.get('cover_thumbnail_urls') or []),
            'video_duration': int((f.get('duration') or 0) / 1000),
            'view_count': int(f.get('view_count') or 0),
            'like_count': int(f.get('like_count') or 0),
            'comment_count': int(f.get('comment_count') or 0),
            'share_count': int(f.get('share_count') or 0),
            'collect_count': int(f.get('collect_count') or 0),
            'date_posted': date_posted.isoformat(),
        })
    return parsed


def parse_kuaishou_search_videos(items: list, search_keyword: str = '') -> list:
    """Parse list feed thô từ search_video_v2 thành list dict (không ghi DB).

    Mỗi item có sẵn cả author_id (numeric, feed.user_id) lẫn author_eid (suy ra
    từ feed.share_info) — 2 định danh cần thiết để sau này track full profile
    (fetch_one_user_v2 cần eid, fetch_user_hot_post cần numeric id).
    """
    parsed = []
    for f in items:
        photo_id = str(f.get('photo_id') or '')
        if not photo_id:
            continue

        caption = f.get('caption') or f.get('pureTitle') or ''
        timestamp = f.get('timestamp') or 0
        date_posted = datetime.fromtimestamp(timestamp / 1000, tz=tz.utc) if timestamp else datetime.now(tz.utc)

        parsed.append({
            'post_id': photo_id,
            'url': f'https://www.kuaishou.com/short-video/{photo_id}',
            'description': caption[:2000],
            'hashtags': _extract_hashtags(caption),
            'thumbnail_url': _pick_url(f.get('cover_thumbnail_urls') or []),
            'video_duration': int((f.get('duration') or 0) / 1000),
            'author_id': str(f.get('user_id') or ''),
            'author_eid': _extract_author_eid(f.get('share_info') or ''),
            'author_username': f.get('user_name') or '',
            'author_avatar': _pick_url(f.get('headurls') or []),
            'author_is_verified': bool(f.get('verified')),
            'view_count': int(f.get('view_count') or 0),
            'like_count': int(f.get('like_count') or 0),
            'comment_count': int(f.get('comment_count') or 0),
            'share_count': int(f.get('share_count') or 0),
            'collect_count': int(f.get('collect_count') or 0),
            'search_keyword': search_keyword,
            'date_posted': date_posted.isoformat(),
        })
    return parsed
