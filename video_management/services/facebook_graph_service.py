"""
Facebook Graph API Service.

This service uses Facebook Graph API to fetch page metadata including:
- Followers count
- Total posts count
- Page information

Unlike Apify, this provides accurate, official data directly from Facebook.
"""

import logging
import requests
from typing import Dict, Any, Optional
from django.conf import settings

logger = logging.getLogger(__name__)


class FacebookGraphService:
    """
    Facebook Graph API service for fetching page metadata.
    
    This service provides accurate followers count and posts count
    that Apify's facebook-posts-scraper doesn't provide.
    """
    
    BASE_URL = "https://graph.facebook.com/v25.0"
    
    def __init__(self):
        """Initialize Facebook Graph API service."""
        self.app_id = getattr(settings, 'FACEBOOK_APP_ID', '')
        self.app_secret = getattr(settings, 'FACEBOOK_APP_SECRET', '')
        self.access_token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
        
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
        Bước 2: Sử dụng URL gộp (hoặc Batch API) để lấy chính xác số view và tương tác thực tế
        của danh sách ID vừa cào được, sửa lỗi 'views=0' của hàm get_page_posts.
        """
        if not video_ids:
            return {}
            
        try:
            # Bổ sung thêm chỉ số play của Reels để tránh bị hụt view của Reels
            metrics = "post_video_views,post_video_reels_organic_plays"
            # Sử dụng công thức URL gộp tối ưu mà chúng ta đã thống nhất
            fields_str = "reactions.summary(true).limit(0),comments.summary(true).limit(0),shares,insights.metric(post_video_views){name,period,values}"
            
            params = {
                'ids': ','.join(video_ids),
                'fields': fields_str,
                'access_token': self.access_token
            }
            
            response = requests.get(self.BASE_URL, params=params, timeout=20)
            response.raise_for_status()
            res_data = response.json()
            
            metrics_map = {}
            for p_id, p_data in res_data.items():
                # 1. Lấy lượt xem video
                views = 0
                insights_list = p_data.get('insights', {}).get('data', [])
                for metric in insights_list:
                    m_name = metric.get('name')
                    # 🛑 FIX: Check mảng values tránh lỗi IndexError
                    if m_name in ['post_video_views', 'post_video_reels_organic_plays']:
                        values_list = metric.get('values', [])
                        if values_list:  # Nếu mảng có phần tử mới lấy
                            views += values_list[0].get('value', 0)
                
                # 2. Lấy tương tác
                likes = p_data.get('reactions', {}).get('summary', {}).get('total_count', 0)
                comments = p_data.get('comments', {}).get('summary', {}).get('total_count', 0)
                shares = p_data.get('shares', {}).get('count', 0)
                
                metrics_map[p_id] = {
                    'view_count': views,
                    'like_count': likes,
                    'comment_count': comments,
                    'share_count': shares,
                    'raw_json': p_data # Lưu lại để ném vào trường raw_data của Model
                }
            return metrics_map
            
        except Exception as e:
            logger.error(f"❌ Thất bại khi chạy Batch cập nhật Metrics: {str(e)}")
            return {}
        
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
