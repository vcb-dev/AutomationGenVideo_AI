"""Đọc Instagram Business qua Graph API — miễn phí, thay đường TikHub tính tiền.

── Vì sao đi được đường này ────────────────────────────────────────────────────
Instagram Business nối với Facebook Page thì đọc được bằng CHÍNH page token đang dùng cho
Facebook (bảng video_management_managedfacebookpage), không cần khoá mới, không tốn tiền.
Đo thật 16/08/2026 trên 25 fanpage nhiều follower nhất: 14 page có instagram_business_account.

── Lượt xem: có thể chưa lấy được, và điều đó KHÔNG được làm hỏng cả lượt ──────
`/{media-id}/insights?metric=views` đòi quyền `instagram_manage_insights`. Nếu token chưa có
quyền đó, Graph trả `(#10) Application does not have permission`. Đã thử 4 đường không cần
quyền — `fields=video_views`, `fields=views`, `fields=play_count`, insight mức tài khoản —
Graph đều báo field không tồn tại. Meta chỉ cho lượt xem qua insights, không có đường vòng.

Nên hỏng insight thì trả `view_count = None` chứ KHÔNG trả 0: None nghĩa là "không lấy được",
0 nghĩa là "thật sự không ai xem". Gộp hai thứ đó chính là sự cố 27/07–09/08/2026 bên Facebook
— sync mỗi sáng ghi 0 đè lên video đang có view thật, 13 ngày mới có người nhận ra. Phía BE
giữ nguyên số cũ khi nhận None (cùng luật với resolve-view-count.ts).

Ngày token được cấp lại kèm quyền, chính đoạn code này tự có số mà không phải sửa gì.

── Giới hạn tần suất ───────────────────────────────────────────────────────────
Instagram Graph API chặn ~200 lượt gọi/giờ cho mỗi tài khoản IG. Mỗi media cần 1 lượt insight
riêng (Meta không cho gộp insight nhiều media vào một request như Facebook), nên phía gọi phải
giữ `limit` nhỏ — đồng bộ delta vài chục bài mới nhất, đừng quét cả 3.493 bài trong một lượt.
"""

import logging
import re

from django.conf import settings
import requests

logger = logging.getLogger(__name__)

GRAPH_VERSION = str(getattr(settings, 'FACEBOOK_GRAPH_API_VERSION', 'v25.0')).strip() or 'v25.0'
BASE_URL = str(getattr(settings, 'FACEBOOK_GRAPH_BASE_URL', f'https://graph.facebook.com/{GRAPH_VERSION}')).rstrip('/')

# Trường lấy được MIỄN PHÍ, không cần quyền insights.
MEDIA_FIELDS = getattr(
    settings,
    'INSTAGRAM_MEDIA_FIELDS',
    'id,media_type,media_product_type,permalink,timestamp,caption,like_count,comments_count,thumbnail_url,media_url',
)

# Chỉ loại có video mới có lượt xem — gọi insight cho ảnh chỉ tốn lượt và nhận lỗi.
VIDEO_TYPES = {'VIDEO', 'REEL', 'REELS'}

TIMEOUT_S = int(getattr(settings, 'FACEBOOK_GRAPH_TIMEOUT', 30))


def extract_shortcode(permalink: str) -> str:
    """`https://www.instagram.com/reel/DAbc123xyz/` → `DAbc123xyz`.

    Bảng ScraperInstagramReel có cột `shortcode` UNIQUE, mà Graph API không trả trường này —
    chỉ có permalink. Không đọc được thì trả rỗng để phía gọi tự quyết, đừng ném lỗi.
    """
    if not permalink:
        return ''
    m = re.search(r'/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)', permalink)
    return m.group(1) if m else ''


def extract_hashtags(caption: str) -> list:
    """Tách hashtag khỏi caption cho cột `hashtags[]`.

    `\\w` trong Python có tính cả chữ có dấu (re.UNICODE mặc định), nên #vàng #bạc925 vẫn lấy
    được — caption kênh nội bộ dùng hashtag tiếng Việt rất nhiều.
    """
    if not caption:
        return []
    return re.findall(r'#(\w+)', caption)


class InstagramGraphService:
    """Chỉ ĐỌC Graph API và parse. Không đụng DB — BE là nơi duy nhất ghi, giống luồng Facebook."""

    def fetch_owned_account(self, page_id: str, page_token: str):
        """Tài khoản IG Business gắn với một Facebook Page, hoặc None nếu page không nối IG.

        11/25 page không nối — đó là chuyện bình thường, không phải lỗi, nên trả None chứ
        không ném để phía gọi cứ thế duyệt hết danh sách page.
        """
        try:
            r = requests.get(
                f'{BASE_URL}/{page_id}',
                params={
                    'fields': 'instagram_business_account{id,username,name,followers_count,'
                              'media_count,profile_picture_url,biography,website}',
                    'access_token': page_token,
                },
                timeout=TIMEOUT_S,
            )
        except requests.exceptions.RequestException as e:
            logger.warning('[IG] Không hỏi được page %s: %s', page_id, e)
            return None

        if r.status_code != 200:
            logger.warning('[IG] Page %s trả %s', page_id, r.status_code)
            return None

        ig = (r.json() or {}).get('instagram_business_account')
        if not ig:
            return None

        return {
            'instagram_id': str(ig.get('id') or ''),
            'username': ig.get('username') or '',
            'full_name': ig.get('name') or '',
            'url': f"https://www.instagram.com/{ig.get('username') or ''}/",
            'avatar_url': ig.get('profile_picture_url') or '',
            'biography': ig.get('biography') or '',
            'external_url': ig.get('website') or '',
            'followers_count': int(ig.get('followers_count') or 0),
            'posts_count': int(ig.get('media_count') or 0),
            'page_id': str(page_id),
        }

    def fetch_media(self, instagram_id: str, page_token: str, limit: int = 25) -> list:
        """Bài đăng gần nhất của một tài khoản IG, đã chuẩn hoá theo cột của ScraperInstagramReel."""
        try:
            r = requests.get(
                f'{BASE_URL}/{instagram_id}/media',
                params={'fields': MEDIA_FIELDS, 'limit': limit, 'access_token': page_token},
                timeout=TIMEOUT_S,
            )
        except requests.exceptions.RequestException as e:
            logger.warning('[IG] Không lấy được media của %s: %s', instagram_id, e)
            return []

        if r.status_code != 200:
            logger.warning('[IG] Media của %s trả %s', instagram_id, r.status_code)
            return []

        items = []
        missing_views = 0  # đếm để log MỘT dòng, xem _fetch_views
        for m in (r.json() or {}).get('data', []):
            post_id = str(m.get('id') or '')
            if not post_id:
                continue

            caption = m.get('caption') or ''
            permalink = m.get('permalink') or ''
            is_video = (m.get('media_type') or '').upper() in VIDEO_TYPES

            items.append({
                'post_id': post_id,
                'shortcode': extract_shortcode(permalink),
                'url': permalink,
                'description': caption,
                'hashtags': extract_hashtags(caption),
                'thumbnail_url': m.get('thumbnail_url') or m.get('media_url') or '',
                'media_product_type': m.get('media_product_type') or '',
                'likes_count': int(m.get('like_count') or 0),
                'comments_count': int(m.get('comments_count') or 0),
                'date_posted': m.get('timestamp') or '',
                # None = chưa lấy được (thiếu quyền / ảnh không có view), KHÁC 0 = không ai xem.
                'view_count': None,
            })
            if is_video:
                items[-1]['view_count'] = self._fetch_views(post_id, page_token)
                if items[-1]['view_count'] is None:
                    missing_views += 1

        if missing_views:
            # MỘT dòng cho cả tài khoản, không phải mỗi video một dòng: thiếu quyền insights là
            # trạng thái đã biết, log từng bài thì 106 page đẻ ra hơn 1.200 dòng mỗi lượt đồng
            # bộ — ngập log tới mức che mất lỗi thật.
            logger.warning(
                '[IG] %s: không lấy được lượt xem cho %d/%d video (thường do thiếu quyền '
                'instagram_manage_insights)', instagram_id, missing_views, len(items),
            )
        return items

    def _fetch_views(self, media_id: str, page_token: str):
        """Lượt xem của một media. Trả None khi không lấy được — xem ghi chú đầu file."""
        try:
            r = requests.get(
                f'{BASE_URL}/{media_id}/insights',
                params={'metric': 'views', 'access_token': page_token},
                timeout=TIMEOUT_S,
            )
        except requests.exceptions.RequestException:
            return None

        if r.status_code != 200:
            # Im lặng ở đây có chủ đích — fetch_media() đếm rồi log gộp một dòng cho cả tài
            # khoản. Log từng bài là ngập log mà không thêm thông tin gì.
            return None

        for v in (r.json() or {}).get('data', []):
            if v.get('name') == 'views' and v.get('values'):
                return int(v['values'][0].get('value') or 0)
        return None
