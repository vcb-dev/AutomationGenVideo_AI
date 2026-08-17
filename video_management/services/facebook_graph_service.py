"""
Facebook Graph API Service.

This service uses Facebook Graph API to fetch page metadata including:
- Followers count
- Total posts count
- Page information

This provides accurate, official data directly from Facebook.
"""

import logging
import re
import requests
from typing import Dict, Any, Optional
from django.conf import settings

from .facebook_token_store import get_token

logger = logging.getLogger(__name__)

# Dừng ở & hoặc khoảng trắng hoặc nháy — token nằm cuối chuỗi cũng phải che được.
_TOKEN_RE = re.compile(r'(access_token=)[^&\s"\'\\]+')


def _scrub_tokens(text) -> str:
    """Che access_token trước khi ghi log.

    Vì sao cần: `str(e)` của requests nhúng NGUYÊN URL kèm query string, nên mỗi lần request
    Graph API hỏng là token bị ghi thẳng vào log dưới dạng chữ thường. Đo được ngày 09/08/2026:
    log production đã chứa token đầy đủ suốt 13 ngày sự cố lượt xem. Token đó đọc được toàn bộ
    dữ liệu 106 fanpage — ai đọc được log là dùng được luôn.
    """
    return _TOKEN_RE.sub(r'\1<ĐÃ ẨN>', str(text))


class _ScrubTokenFilter(logging.Filter):
    """Lọc ở tầng logger thay vì sửa từng lời gọi.

    Service này có 47 lời gọi log, phần lớn kèm str(e). Sửa tay từng cái thì lần sau ai thêm
    một dòng log mới là hở lại, mà không ai nhớ nổi quy tắc. Đặt ở đây thì quên cũng vẫn an toàn.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _scrub_tokens(record.msg)
        if record.args:
            record.args = tuple(
                _scrub_tokens(a) if isinstance(a, str) else a for a in record.args
            )
        return True


logger.addFilter(_ScrubTokenFilter())


# Metric dùng để tính lượt xem. CẢNH BÁO trước khi thêm vào đây: Facebook từ chối NGUYÊN request
# `insights.metric(a,b)` nếu chỉ một metric không hợp lệ — nên một metric bị khai tử kéo sập luôn
# các metric còn sống chung request.
#
# Đó đúng là sự cố 27/07–09/08/2026: `post_video_reels_organic_plays` bị khai tử, kéo theo
# `post_video_views` (vẫn chạy tốt) cũng không lấy được, và 1.169 video bị ghi view = 0 dù lượt
# xem thật vẫn nằm sẵn ở Facebook. Đo bằng page token thật ngày 09/08/2026:
#
#     post_video_views                 ✅ 486 / 902 / 649 / 98 / 585 trên 5 bài
#     post_video_reels_organic_plays   ❌ (#100) The value must be a valid insights metric
#     blue_reels_total_plays           ❌ (#100)   — không có bản thay thế cùng tên cho Reels
#     post_reels_plays                 ❌ (#100)
#
# Thêm metric mới thì thử riêng từng cái bằng /{post_id}/insights?metric=<tên> trước đã.
VIEW_METRICS = ('post_video_views',)


class FacebookGraphService:
    """
    Facebook Graph API service for fetching page metadata.
    
    This service provides accurate followers count and posts count
    that scrapers typically don't provide.
    """
    @property
    def BASE_URL(self) -> str:
        return getattr(
            settings,
            'FACEBOOK_GRAPH_BASE_URL',
            f"https://graph.facebook.com/{getattr(settings, 'FACEBOOK_GRAPH_API_VERSION', 'v25.0')}",
        ).rstrip('/')

    def __init__(self):
        """Initialize Facebook Graph API service."""
        self.app_id = getattr(settings, 'FACEBOOK_APP_ID', '')
        self.app_secret = getattr(settings, 'FACEBOOK_APP_SECRET', '')
        # Qua token store, KHÔNG đọc thẳng settings: settings nạp một lần lúc boot nên
        # token vừa gia hạn sẽ không có hiệu lực tới lần restart Django.
        self.access_token = get_token()
        
        if not self.app_id or not self.app_secret:
            raise ValueError("FACEBOOK_APP_ID and FACEBOOK_APP_SECRET must be configured")
        
        # If no access token, generate app access token
        if not self.access_token:
            self.access_token = self._generate_app_access_token()
        
        logger.info("Initialized Facebook Graph API service")
    
    def _generate_app_access_token(self) -> str:
        """
        Generate app access token using app ID and secret.
        
        This token can access public page data without user login.
        
        Returns:
            Access token string
        """
        try:
            url = f"{self.BASE_URL}/oauth/access_token"
            params = {
                'client_id': self.app_id,
                'client_secret': self.app_secret,
                'grant_type': 'client_credentials'
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            token = data.get('access_token', '')
            
            if token:
                logger.info("✅ Generated app access token successfully")
                return token
            else:
                raise ValueError("No access token in response")
                
        except Exception as e:
            logger.error(f"Failed to generate app access token: {str(e)}")
            raise
    
    def get_page_metadata(self, page_id: str) -> Dict[str, Any]:
        """
        Get page metadata including followers and posts count.
        
        Args:
            page_id: Facebook page ID or username
            
        Returns:
            Dictionary with page metadata:
            {
                'page_id': str,
                'name': str,
                'followers_count': int,
                'fan_count': int,  # Same as followers
                'posts_count': int,
                'category': str,
                'about': str,
                'website': str,
                'picture_url': str
            }
        """
        try:
            logger.info(f"🔍 Fetching metadata for page: {page_id}")
            
            url = f"{self.BASE_URL}/{page_id}"
            
            # Request fields
            fields = [
                'id',
                'name',
                'followers_count',
                'fan_count',
                'posts.limit(0).summary(true)',  # Get total count without fetching posts
                'category',
                'about',
                'website',
                'picture'
            ]
            
            params = {
                'fields': ','.join(fields),
                'access_token': self.access_token
            }
            
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            
            # Extract data
            result = {
                'page_id': data.get('id', page_id),
                'name': data.get('name', ''),
                'followers_count': data.get('followers_count', 0),
                'fan_count': data.get('fan_count', 0),
                'posts_count': 0,
                'category': data.get('category', ''),
                'about': data.get('about', ''),
                'website': data.get('website', ''),
                'picture_url': ''
            }
            
            # Get posts count from summary
            if 'posts' in data and 'summary' in data['posts']:
                result['posts_count'] = data['posts']['summary'].get('total_count', 0)
            
            # Get picture URL
            if 'picture' in data and 'data' in data['picture']:
                result['picture_url'] = data['picture']['data'].get('url', '')
            
            logger.info(f"✅ Page metadata retrieved:")
            logger.info(f"  - Name: {result['name']}")
            logger.info(f"  - Followers: {result['followers_count']:,}")
            logger.info(f"  - Posts: {result['posts_count']:,}")
            
            return result
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403:
                logger.error("❌ Access denied. Check your access token permissions.")
                logger.error("   Required: pages_read_engagement, pages_show_list")
            elif e.response.status_code == 404:
                logger.error(f"❌ Page not found: {page_id}")
            else:
                logger.error(f"❌ HTTP error: {e.response.status_code} - {e.response.text}")
            raise
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch page metadata: {str(e)}", exc_info=True)
            raise
    
    def get_page_posts(self, page_id: str, max_results: int = 100, since_date: Optional[str] = None, access_token: Optional[str] = None) -> list:
        """
        Get posts from a Facebook page.
        
        Args:
            page_id: Facebook page ID or username
            max_results: Maximum number of posts to fetch
            since_date: Fetch posts since this date (YYYY-MM-DD)
            
        Returns:
            List of posts with engagement data
        """
        try:
            logger.info(f"📝 Fetching posts for page: {page_id}")
            
            url = f"{self.BASE_URL}/{page_id}/feed"
            
            # QUAN TRỌNG: Phải khai báo media{source,image} đích danh.
            # Nếu chỉ ghi "media" Facebook trả thumbnail, KHÔNG trả link .mp4 có tiếng.
            # media{source} = Progressive MP4 (video + audio muxed) — link stream chuẩn.
            attachments_fields = (
                'attachments{'
                    'type,title,description,media_type,url,'
                    'media{source,image},'
                    'subattachments{type,media_type,url,media{source,image}}'
                '}'
            )
            fields = [
                'id',
                'message',
                'created_time',
                'permalink_url',
                'full_picture',
                'shares',
                attachments_fields,
            ]
            
            active_token = access_token if access_token else self.access_token
            
            params = {
                'fields': ','.join(fields),
                'limit': min(max_results, 100),
                'access_token': active_token
            }
            
            # Add date filter if provided
            if since_date:
                from datetime import datetime
                try:
                    dt = datetime.strptime(since_date, '%Y-%m-%d')
                    params['since'] = int(dt.timestamp())
                    logger.info(f"Filtering posts since: {since_date}")
                except:
                    pass
            
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            posts = data.get('data', [])
            
            # Normalize posts data
            normalized_posts = []
            for post in posts:
                normalized = self._normalize_post(post)
                if normalized:
                    normalized_posts.append(normalized)
            
            logger.info(f"✅ Fetched {len(normalized_posts)} posts")
            return normalized_posts
            
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ Facebook API Error Details: {e.response.text}")
            return []
        except Exception as e:
            logger.error(f"Failed to fetch page posts: {str(e)}", exc_info=True)
            return []

    def get_page_posts_deep(
        self,
        page_id: str,
        max_total: int = 300,
        page_size: int = 50,
        access_token: Optional[str] = None,
        cooldown: float = 1.5,
    ) -> list:
        """Cào sâu lịch sử bài viết bằng cách lần theo paging.next.

        Dùng cho Giai đoạn 1 (Initial Backfill). Tích hợp Dual Cooldown:
        - Nghỉ `cooldown` giây giữa mỗi trang phân trang
        - Tự dừng khi hết data hoặc đạt max_total
        """
        import time

        attachments_fields = (
            'attachments{'
                'type,title,description,media_type,url,'
                'media{source,image},'
                'subattachments{type,media_type,url,media{source,image}}'
            '}'
        )
        fields = [
            'id', 'message', 'created_time', 'permalink_url',
            'full_picture', 'shares', attachments_fields,
        ]

        active_token = access_token or self.access_token
        url = f"{self.BASE_URL}/{page_id}/feed"
        params = {
            'fields': ','.join(fields),
            'limit': min(page_size, 100),
            'access_token': active_token,
        }

        all_posts = []
        page_num = 0

        while url and len(all_posts) < max_total:
            page_num += 1
            try:
                response = requests.get(url, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()
            except requests.exceptions.HTTPError as e:
                logger.error(f"❌ Backfill trang {page_num} lỗi: {e.response.text[:200]}")
                break
            except Exception as e:
                logger.error(f"❌ Backfill trang {page_num} exception: {e}")
                break

            posts = data.get('data', [])
            if not posts:
                logger.info(f"📭 Backfill: hết bài ở trang {page_num}")
                break

            for post in posts:
                normalized = self._normalize_post(post)
                if normalized:
                    all_posts.append(normalized)

            logger.info(f"📄 Backfill trang {page_num}: +{len(posts)} bài (tổng: {len(all_posts)})")

            # Lấy URL trang tiếp theo
            next_url = data.get('paging', {}).get('next')
            if not next_url or len(all_posts) >= max_total:
                break

            url = next_url
            params = {}  # next_url đã chứa sẵn params

            # Dual Cooldown: nghỉ giữa mỗi trang phân trang
            time.sleep(cooldown)

        logger.info(f"✅ Backfill hoàn tất: {len(all_posts)} bài từ {page_num} trang")
        return all_posts[:max_total]

    # ─── Video URL extraction ─────────────────────────────

    @staticmethod
    def _extract_video_source(post: Dict[str, Any]) -> tuple:
        """
        Bóc tách video URL (.mp4 có tiếng) từ attachments của một post.

        Kiểm tra 2 tầng:
          1. Attachment trực tiếp (video_inline, video_autoplay, …)
          2. Subattachments (album chứa nhiều video)

        Returns:
            (video_url, thumbnail_url, is_video)
        """
        video_url = ''
        thumbnail = post.get('full_picture', '')
        is_video = False

        attachments_data = post.get('attachments', {}).get('data', [])
        if not attachments_data:
            return video_url, thumbnail, is_video

        for att in attachments_data:
            att_type = att.get('type', '')
            media_type = att.get('media_type', '')
            media = att.get('media', {})

            # ── Tầng 1: Video trực tiếp ──
            if att_type.startswith('video') or media_type == 'video':
                is_video = True
                source = media.get('source', '')
                if source:
                    video_url = source

                logger.info(
                    f"  📎 att type={att_type} media_type={media_type} | "
                    f"source={'✓ ' + source[:80] if source else '✗'} | "
                    f"media keys={list(media.keys())}"
                )

            # ── Tầng 2: Subattachments (album) ──
            for sub in att.get('subattachments', {}).get('data', []):
                sub_type = sub.get('type', '')
                sub_media_type = sub.get('media_type', '')
                sub_media = sub.get('media', {})

                if sub_type.startswith('video') or sub_media_type == 'video':
                    is_video = True
                    sub_source = sub_media.get('source', '')
                    if sub_source and not video_url:
                        video_url = sub_source

            # ── Thumbnail ──
            att_image = media.get('image', {})
            if att_image.get('src'):
                thumbnail = att_image['src']

        return video_url, thumbnail, is_video

    # ─── Post normalization ─────────────────────────────

    def _normalize_post(self, post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize Graph API post data to common format."""
        try:
            from datetime import datetime

            post_id = post.get('id', '')
            message = post.get('message', '')
            created_time = post.get('created_time', '')
            permalink = post.get('permalink_url', '')

            timestamp = 0
            if created_time:
                try:
                    # Facebook trả format "2026-05-09T07:30:13+0000" — cần chuẩn hóa timezone
                    normalized_time = created_time.replace('Z', '+00:00')
                    # Fix "+0000" → "+00:00" cho fromisoformat
                    if normalized_time.endswith('+0000'):
                        normalized_time = normalized_time[:-5] + '+00:00'
                    elif normalized_time.endswith('-0000'):
                        normalized_time = normalized_time[:-5] + '+00:00'
                    dt = datetime.fromisoformat(normalized_time)
                    timestamp = int(dt.timestamp())
                except Exception:
                    pass

            # Engagement
            likes_count = 0
            if 'likes' in post and 'summary' in post['likes']:
                likes_count = post['likes']['summary'].get('total_count', 0)

            comments_count = 0
            if 'comments' in post and 'summary' in post['comments']:
                comments_count = post['comments']['summary'].get('total_count', 0)

            shares_count = 0
            if 'shares' in post:
                shares_count = post['shares'].get('count', 0)

            # Video URL extraction (hàm chuyên dụng)
            video_url, thumbnail, is_video = self._extract_video_source(post)

            if post.get('type') == 'video':
                is_video = True

            return {
                'id': post_id,
                'video_id': post_id,
                'title': message[:200] if message else 'No message',
                'description': message,
                'created_time': created_time,
                'timestamp': timestamp,
                'url': permalink,
                'video_url': video_url,
                'download_url': video_url,
                'thumbnail': thumbnail,
                'thumbnail_url': thumbnail,
                'is_video': is_video,
                'isVideo': is_video,
                'likes': likes_count,
                'like_count': likes_count,
                'likes_count': likes_count,
                'comments': comments_count,
                'comment_count': comments_count,
                'comments_count': comments_count,
                'shares': shares_count,
                'share_count': shares_count,
                'shares_count': shares_count,
                'views': 0,
                'view_count': 0,
                'views_count': 0,
                'raw_data': post,
            }

        except Exception as e:
            logger.error(f"Failed to normalize post: {str(e)}")
            return None
    
    def get_page_insights(self, page_id: str, metrics: Optional[list] = None) -> Dict[str, Any]:
        """
        Get page insights (analytics).
        
        Note: This requires a Page Access Token, not App Access Token.
        
        Args:
            page_id: Facebook page ID
            metrics: List of metrics to fetch (e.g., ['page_impressions', 'page_engaged_users'])
            
        Returns:
            Dictionary with insights data
        """
        if not metrics:
            metrics = ['page_impressions', 'page_engaged_users', 'page_views_total']
        
        try:
            url = f"{self.BASE_URL}/{page_id}/insights"
            params = {
                'metric': ','.join(metrics),
                'access_token': self.access_token
            }
            
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            return data
            
        except Exception as e:
            logger.error(f"Failed to fetch page insights: {str(e)}")
            raise
    
    def test_connection(self) -> bool:
        """
        Test if the service is properly configured and can connect to Facebook.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Try to get info about Facebook's own page
            url = f"{self.BASE_URL}/facebook"
            params = {
                'fields': 'id,name',
                'access_token': self.access_token
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"✅ Connection test successful! Got data: {data}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Connection test failed: {str(e)}")
            return False
        
    def update_video_views_batch(self, video_ids: list) -> Dict[str, Dict[str, int]]:
        """
        Bước 2: Lấy reactions/comments/shares (luôn có với MỌI Page Post, kể cả bài đăng
        không phải video — ảnh/status/link chia sẻ) + views (CHỈ áp dụng cho bài có
        video, fetch RIÊNG). Tách 2 request vì nếu 1 bài trong batch không có video,
        Facebook trả lỗi cứng cho field 'insights' và làm sập luôn cả batch — lỗi ở
        bước views không được phép làm mất reactions/comments/shares đã lấy được.
        """
        if not video_ids:
            return {}

        try:
            fields_str = "reactions.summary(true).limit(0),comments.summary(true).limit(0),shares"
            params = {
                'ids': ','.join(video_ids),
                'fields': fields_str,
                'access_token': self.access_token,
            }
            response = requests.get(self.BASE_URL, params=params, timeout=20)
            response.raise_for_status()
            res_data = response.json()
        except requests.exceptions.HTTPError as e:
            body = e.response.text if e.response is not None else ''
            logger.error(f"❌ Thất bại khi lấy reactions/comments/shares: {str(e)} | body: {body}")
            return {}
        except Exception as e:
            logger.error(f"❌ Thất bại khi lấy reactions/comments/shares: {str(e)}")
            return {}

        # Views: best-effort, không chặn kết quả reactions/comments/shares nếu fail
        # (bài không có video, hoặc metric không áp dụng cho object này).
        #
        # views_fetch_ok phân biệt hai thứ mà bản cũ gộp làm một, và chính chỗ gộp đó gây ra sự
        # cố 27/07–09/08/2026: request insights hỏng thì mọi bài đều nhận view_count = 0, không
        # cách nào phân biệt với 0 nghĩa là THẬT SỰ không ai xem. Kết quả: 13 ngày liền dashboard
        # vẽ đường lượt xem tụt về 0 trông y như dữ liệu thật, trong khi like/comment/share vẫn
        # về đều — không ai báo động vì bảng vẫn đầy số.
        #
        # Hỏng thì trả None. Phía BE (facebook-owned-pages.service.ts) đã có sẵn nhánh
        # `m.view_count ?? view_count_cũ` để giữ nguyên số cũ — nhánh đó viết ra chính là cho
        # tình huống này, chỉ chưa bao giờ chạy vì Python luôn gửi 0.
        views_map: Dict[str, int] = {}
        views_fetch_ok = False
        try:
            insights_params = {
                'ids': ','.join(video_ids),
                # Lấy tên metric từ VIEW_METRICS chứ không gõ cứng: gõ cứng ở đây và ở vòng lặp
                # đọc kết quả bên dưới là hai nơi, đổi một nơi quên nơi kia thì view âm thầm về 0.
                'fields': f"insights.metric({','.join(VIEW_METRICS)}){{name,period,values}}",
                'access_token': self.access_token,
            }
            insights_resp = requests.get(self.BASE_URL, params=insights_params, timeout=20)
            insights_resp.raise_for_status()
            insights_data = insights_resp.json()
            for p_id, p_data in insights_data.items():
                views = 0
                for metric in p_data.get('insights', {}).get('data', []):
                    if metric.get('name') in VIEW_METRICS:
                        values_list = metric.get('values', [])
                        if values_list:
                            views += values_list[0].get('value', 0)
                views_map[p_id] = views
            views_fetch_ok = True
        except requests.exceptions.HTTPError as e:
            # ERROR chứ không phải WARNING, và giữ nguyên `body`: body là thứ DUY NHẤT nói được
            # Facebook từ chối vì lý do gì (metric bị khai tử / thiếu quyền / token chết). Mức
            # warning đã khiến sự cố lần trước chìm 13 ngày không ai thấy.
            body = e.response.text if e.response is not None else ''
            logger.error(
                f"❌ Không lấy được views cho {len(video_ids)} bài — view_count để TRỐNG "
                f"(không ghi 0 đè lên số cũ): {str(e)} | body: {body}"
            )
        except Exception as e:
            logger.error(
                f"❌ Không lấy được views cho {len(video_ids)} bài — view_count để TRỐNG: {str(e)}"
            )

        metrics_map = {}
        for p_id, p_data in res_data.items():
            likes = p_data.get('reactions', {}).get('summary', {}).get('total_count', 0)
            comments = p_data.get('comments', {}).get('summary', {}).get('total_count', 0)
            shares = p_data.get('shares', {}).get('count', 0)

            metrics_map[p_id] = {
                # Request thành công mà bài không có khối insights = bài đó thật sự không có
                # video để đếm view → 0 là câu trả lời đúng. Request hỏng → None = "không biết".
                'view_count': views_map.get(p_id, 0) if views_fetch_ok else None,
                'like_count': likes,
                'comment_count': comments,
                'share_count': shares,
                'raw_json': p_data,  # Lưu lại để ném vào trường raw_data của Model
            }
        return metrics_map

    def update_video_node_metrics_batch(self, video_ids: list, access_token: Optional[str] = None) -> Dict[str, Dict[str, int]]:
        """Lấy views/likes/comments cho ID Video/Reels NODE THUẦN (vd link facebook.com/reel/{id}
        dán tay, không sync qua /feed) — KHÁC update_video_views_batch() ở trên vốn dành cho
        Page Post ID (dạng {page_id}_{post_id}).

        Video node dùng edge "video_insights" (không phải "insights") để lấy views, và
        KHÔNG có field "shares"/"reactions" như Post — chỉ có "likes". Gọi nhầm field Post
        vào 1 ID Video node sẽ bị Facebook trả 400 "(#100) Tried accessing nonexisting field".
        Views fetch riêng (best-effort) — lỗi/rỗng ở bước này không mất likes/comments.
        """
        if not video_ids:
            return {}

        token = access_token or self.access_token
        try:
            fields_str = "likes.summary(true).limit(0),comments.summary(true).limit(0)"
            params = {
                'ids': ','.join(video_ids),
                'fields': fields_str,
                'access_token': token,
            }
            response = requests.get(self.BASE_URL, params=params, timeout=20)
            response.raise_for_status()
            res_data = response.json()
        except requests.exceptions.HTTPError as e:
            body = e.response.text if e.response is not None else ''
            logger.error(f"❌ Thất bại khi lấy likes/comments Video node: {str(e)} | body: {body}")
            return {}
        except Exception as e:
            logger.error(f"❌ Thất bại khi lấy likes/comments Video node: {str(e)}")
            return {}

        views_map: Dict[str, int] = {}
        try:
            insights_params = {
                'ids': ','.join(video_ids),
                # blue_reels_play_count: metric "Lượt phát" thật của Reels — total_video_views
                # (chỉ số video thường) thường RỖNG với Reels nên phải xin cả 2, cộng dồn cái có data.
                'fields': "video_insights.metric(total_video_views,blue_reels_play_count){name,period,values}",
                'access_token': token,
            }
            insights_resp = requests.get(self.BASE_URL, params=insights_params, timeout=20)
            insights_resp.raise_for_status()
            insights_data = insights_resp.json()
            for v_id, v_data in insights_data.items():
                views = 0
                for metric in v_data.get('video_insights', {}).get('data', []):
                    if metric.get('name') in ('total_video_views', 'blue_reels_play_count'):
                        values_list = metric.get('values', [])
                        if values_list:
                            views += values_list[0].get('value', 0)
                views_map[v_id] = views
                if views == 0:
                    logger.info(f"ℹ️ [VIDEO-INSIGHTS] {v_id} không có total_video_views/blue_reels_play_count — raw: {v_data.get('video_insights')}")
        except requests.exceptions.HTTPError as e:
            body = e.response.text if e.response is not None else ''
            logger.warning(f"⚠️ Không lấy được video_insights: {str(e)} | body: {body}")
        except Exception as e:
            logger.warning(f"⚠️ Không lấy được video_insights: {str(e)}")

        metrics_map = {}
        for v_id, v_data in res_data.items():
            likes = v_data.get('likes', {}).get('summary', {}).get('total_count', 0)
            comments = v_data.get('comments', {}).get('summary', {}).get('total_count', 0)

            metrics_map[v_id] = {
                'view_count': views_map.get(v_id, 0),
                'like_count': likes,
                'comment_count': comments,
                'share_count': 0,  # Video node không có edge "shares" (chỉ Page Post mới có)
                'raw_json': v_data,
            }
        return metrics_map

    def get_my_managed_pages(self, access_token: Optional[str] = None) -> list:
        """
        Fetch the list of pages that the user owns/manages.
        
        Uses the /me/accounts endpoint to get user's managed pages.
        
        Args:
            access_token: User access token (not app token). If None, uses self.access_token
            
        Returns:
            List of page dictionaries with structure:
            [
                {
                    'id': '123456',
                    'name': 'Page Name',
                    'username': 'page_username',
                    'category': 'Brand',
                    'access_token': 'page_access_token_...',
                    'picture': {'data': {'height': 50, 'width': 50, 'url': '...'}},
                    'raw_data': {...}
                },
                ...
            ]
        """
        token = access_token or self.access_token
        
        if not token:
            logger.error("❌ No access token provided to get_my_managed_pages")
            return []
        
        try:
            url = f"{self.BASE_URL}/me/accounts"
            
            fields = [
                'id',
                'name',
                'username',
                'category',
                'category_list',
                'access_token',
                'picture{url,width,height}',
                'website',
                'about',
                'emails',
                'founded',
                'mission'
            ]
            
            params = {
                'fields': ','.join(fields),
                'limit': 100,
                'access_token': token
            }
            
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            pages = data.get('data', [])
            
            logger.info(f"✅ Fetched {len(pages)} managed pages for user")
            for page in pages:
                logger.info(f"   - {page.get('name')} (ID: {page.get('id')})")
            
            return pages
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401 or e.response.status_code == 403:
                logger.error("❌ Access denied. Check token permissions.")
                logger.error("   Required: pages_show_list, pages_read_engagement")
            else:
                logger.error(f"❌ HTTP error: {e.response.status_code} - {e.response.text}")
            return []
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch managed pages: {str(e)}", exc_info=True)
            return []

    def get_page_details(self, page_id: str, fields: str = "fan_count,followers_count", access_token: Optional[str] = None) -> Optional[dict]:
        """
        Gọi Graph API lấy thông tin chi tiết (fields) của một Page cụ thể.
        Mặc định lấy fan_count (Likes) và followers_count (Followers).
        """
        # Ưu tiên dùng token truyền riêng cho page, nếu không có thì dùng token mặc định của class
        token_to_use = access_token or self.access_token
        
        if not token_to_use:
            logger.error(f"❌ Không thể lấy details cho Page {page_id} do thiếu Access Token.")
            return None

        url = f"{self.BASE_URL}/{page_id}"
        params = {
            'fields': fields,
            'access_token': token_to_use
        }

        try:
            response = requests.get(url, params=params, timeout=10)
            
            # Nếu Facebook trả về lỗi (4xx, 5xx), dòng này sẽ ném ra ngoại lệ để catch bên dưới
            response.raise_for_status() 
            
            return response.json()
            
        except requests.exceptions.HTTPError as http_err:
            logger.error(f"❌ Facebook API Error cho Page {page_id}: {response.text}")
            return None
        except Exception as err:
            logger.error(f"❌ Lỗi kết nối khi gọi chi tiết Page {page_id}: {str(err)}")
            return None

# Convenience function
def get_facebook_page_metadata(page_id: str) -> Dict[str, Any]:
    """
    Convenience function to get Facebook page metadata.
    
    Args:
        page_id: Facebook page ID or username
        
    Returns:
        Dictionary with page metadata
    """
    service = FacebookGraphService()
    return service.get_page_metadata(page_id)
