import logging
from typing import Optional
from django.utils import timezone

from .facebook_graph_service import FacebookGraphService
from ..models import ManagedFacebookPage, OwnedVideoContent

logger = logging.getLogger(__name__)


class FacebookFetcher:
    """Helper to fetch page metadata + posts and persist to DB.

    - Upserts `ManagedFacebookPage` metadata
    - Creates/updates `OwnedVideoContent` for posts (videos)
    """

    def __init__(self):
        self.graph = FacebookGraphService()

    @staticmethod
    def _parse_published_at(post_data: dict):
        """Parse thời gian đăng bài từ Facebook.

        Ưu tiên:
        1. created_time (ISO string từ Graph API) — chính xác nhất
        2. timestamp (unix int đã parse) — fallback
        3. timezone.now() — fallback cuối nếu không có data
        """
        from datetime import datetime as dt, timezone as tz_dt

        created_time = post_data.get('created_time', '')
        if created_time:
            try:
                normalized = created_time.replace('Z', '+00:00')
                if normalized.endswith('+0000'):
                    normalized = normalized[:-5] + '+00:00'
                return dt.fromisoformat(normalized)
            except (ValueError, TypeError):
                pass

        timestamp = post_data.get('timestamp', 0)
        if timestamp and timestamp > 0:
            return dt.fromtimestamp(timestamp, tz=tz_dt.utc)

        return timezone.now()
    
    def import_my_managed_pages(self, user_access_token: Optional[str] = None) -> dict:
        """
        Quét các Page chính chủ và tự động lưu vào DB.
        Nếu không truyền user_access_token, hàm sẽ tự lấy token vĩnh viễn trong .env
        """
        # Nếu không truyền, tự lấy token tổng trong settings (.env)
        token_to_use = user_access_token or self.graph.access_token
        
        if not token_to_use:
            logger.error("❌ Không tìm thấy FACEBOOK_ACCESS_TOKEN trong settings hoặc .env")
            return {'created': 0, 'updated': 0}

        # Gọi API /me/accounts
        raw_pages = self.graph.get_my_managed_pages(token_to_use)
        if not raw_pages:
            logger.warning("⚠️ Không lấy được danh sách Page (Token hết hạn hoặc sai quyền).")
            return {'created': 0, 'updated': 0}

        created_count = 0
        updated_count = 0
        for p in raw_pages:
            page_id = p.get('id')
            if not page_id:
                continue

            # Toán tử (p.get('picture') or {}) đảm bảo nếu là None hoặc không có key thì đều biến thành dict rỗng {}
            avatar_url = (p.get('picture') or {}).get('data', {}).get('url', '')
            page_token = p.get('access_token')  # Token vĩnh viễn của riêng Page này

            # Tiến hành lưu/cập nhật vào DB
            page_obj, created = ManagedFacebookPage.objects.update_or_create(
                page_id=str(page_id),
                defaults={
                    'name': p.get('name', ''),
                    'username': p.get('username') or page_id,
                    'category': p.get('category', ''),
                    'avatar_url': avatar_url,
                    'page_access_token': page_token,  # Lưu lại để dùng cho hàm sync_all_managed()
                    'is_active': True,
                    'raw_data': p,
                }
            )
            if created:
                created_count += 1
                logger.info(f"➕ Đã nạp Page mới: {page_obj.name}")
            else:
                updated_count += 1
                logger.info(f"🔄 Đã cập nhật token cho Page: {page_obj.name}")

            if avatar_url and not page_obj.avatar_drive_url:
                from ..tasks import upload_thumbnail_to_drive_task
                upload_thumbnail_to_drive_task.delay(
                    model='facebook_managed_avatar',
                    object_id=page_obj.id,
                    cdn_url=avatar_url,
                    filename=f'fb-managed-avatar-{page_obj.id}.jpg',
                )
        # Sau khi lưu hết danh sách Page vào DB, kích hoạt hàm refresh số liệu luôn
        if created_count > 0 or updated_count > 0:
            logger.info("🚀 Kích hoạt làm tươi số liệu ngay sau khi import...")
            self.refresh_all_pages_metrics()
            
        return {'created': created_count, 'updated': updated_count}
    
    def refresh_all_pages_metrics(self) -> dict:
        """
        Quét và cập nhật số lượng Likes và Followers cho tất cả active pages.
        Dùng cho background worker - gọi mỗi ngày/giờ để làm tươi dữ liệu.
        """
        pages = ManagedFacebookPage.objects.filter(is_active=True)
        logger.info(f"🔄 Đang cập nhật số liệu Likes/Followers cho {pages.count()} pages...")
        
        updated_count = 0
        failed_count = 0
        errors = []
        
        original_token = getattr(self.graph, 'access_token', None)
        
        for page in pages:
            try:
                # Lấy token đã giải mã từ model
                decrypted_token = page.get_decrypted_token()
                if not decrypted_token:
                    failed_count += 1
                    error_msg = f"Không thể giải mã token cho page {page.page_id}"
                    logger.error(error_msg)
                    errors.append(error_msg)
                    continue
                
                # Override token bằng token riêng của Page (đã giải mã)
                self.graph.access_token = decrypted_token
                
                page_data = self.graph.get_page_details(page.page_id, fields="fan_count,followers_count")
                
                if not page_data:
                    logger.warning(f"⚠️ Không lấy được thông tin cho page {page.name}")
                    failed_count += 1
                    continue
                
                # Bóc tách trực tiếp cực kỳ sạch sẽ
                likes = page_data.get('fan_count', 0)
                followers = page_data.get('followers_count', 0)
                
                # Cập nhật vào DB
                page.likes_count = likes
                page.followers_count = followers
                
                # Lưu trữ luôn cục data mới vào raw_data nếu cần
                if page.raw_data is None:
                    page.raw_data = {}
                page.raw_data['latest_metrics'] = {'fan_count': likes, 'followers_count': followers}
                
                # Thêm 'likes_count' vào update_fields
                page.save(update_fields=['likes_count', 'followers_count', 'raw_data', 'updated_at'])
                
                updated_count += 1
                logger.info(f"✅ Updated {page.name}: {likes} Likes | {followers} Followers")
                
            except Exception as e:
                failed_count += 1
                error_msg = f"Thất bại khi cập nhật số liệu Page {page.page_id}: {str(e)}"
                logger.exception(error_msg)
                errors.append(error_msg)
                
        # Khôi phục token gốc
        if original_token:
            self.graph.access_token = original_token
            
        summary = {
            'updated': updated_count,
            'failed': failed_count,
            'errors': errors,
            'total': pages.count(),
        }
        logger.info(f"📊 Tổng kết cập nhật: {summary}")
        return summary

    def sync_page(self, page_id: str, page_access_token: Optional[str] = None, max_posts: int = 50) -> dict:
        """Fetch metadata and posts for a single page and upsert DB records.

        Returns a summary dict: {'page_id':..., 'managed_created': bool, 'created': int, 'updated': int}
        """
        logger.info(f"Syncing Facebook page: {page_id}")

        # Nếu không truyền token, tự lấy Page Access Token đã lưu trong DB
        if not page_access_token:
            try:
                stored_page = ManagedFacebookPage.objects.get(page_id=str(page_id))
                page_access_token = stored_page.get_decrypted_token()
                if not page_access_token:
                    logger.error(f"❌ Không thể giải mã token cho page {page_id}")
            except ManagedFacebookPage.DoesNotExist:
                logger.warning(f"⚠️ Page {page_id} chưa được import, dùng User Access Token mặc định")

        # If a page-specific token is provided, temporarily override service token
        original_token = getattr(self.graph, 'access_token', None)
        if page_access_token:
            self.graph.access_token = page_access_token

        try:
            meta = self.graph.get_page_metadata(page_id)

            # Upsert ManagedFacebookPage
            avatar_url = meta.get('picture_url', '')
            page_obj, created = ManagedFacebookPage.objects.update_or_create(
                page_id=str(meta.get('page_id') or page_id),
                defaults={
                    'name': meta.get('name', ''),
                    'username': meta.get('page_id') if not meta.get('username') else meta.get('username'),
                    'category': meta.get('category', ''),
                    'avatar_url': avatar_url,
                    'followers_count': meta.get('followers_count', 0) or 0,
                    'likes_count': meta.get('fan_count', 0) or 0,
                    'raw_data': meta,
                }
            )

            if avatar_url and not page_obj.avatar_drive_url:
                from ..tasks import upload_thumbnail_to_drive_task
                upload_thumbnail_to_drive_task.delay(
                    model='facebook_managed_avatar',
                    object_id=page_obj.id,
                    cdn_url=avatar_url,
                    filename=f'fb-managed-avatar-{page_obj.id}.jpg',
                )

            logger.info(f"Upserted ManagedFacebookPage: {page_obj} (created={created})")

            # Fetch posts
            raw_posts = self.graph.get_page_posts(
                page_obj.page_id, 
                max_results=max_posts,
                access_token=page_access_token  # Dùng token riêng nếu có
                )
            
            # LỌC: Chỉ giữ lại các bài viết là VIDEO hoặc REELS
            video_posts = [p for p in raw_posts if p.get('is_video') is True]
            if not video_posts:
                logger.info(f"➔ Page này không có video mới nào trong {max_posts} bài gần nhất.")
                page_obj.mark_synced()
                return {'page_id': page_obj.page_id, 'managed_created': bool(created), 'created': 0, 'updated': 0}

            # 3. GOM ID & GỌI BATCH (URL GỘP) ĐỂ LẤY VIEW/INTERACTION THẬT
            video_ids = [p.get('id') for p in video_posts if p.get('id')]
            
            # Gọi hàm cập nhật batch từ Service (hàm chúng ta tối ưu ở câu hỏi trước)
            # Nếu bạn chưa đem hàm này vào Service, hãy bổ sung nó vào class FacebookGraphService nhé
            real_metrics_map = self.graph.update_video_views_batch(video_ids)

            # 4. TIẾN HÀNH LƯU VÀO DATABASE
            created_count = 0
            updated_count = 0
            
            for p in video_posts:
                post_id = p.get('id')
                if not post_id:
                    continue

                # Giữ raw_data gốc từ /feed (chứa created_time, attachments, etc.)
                likes = p.get('like_count', 0)
                comments = p.get('comment_count', 0)
                shares = p.get('share_count', 0)
                views = 0
                raw_json = p.get('raw_data') or p

                # Merge batch metrics vào raw_json (KHÔNG ghi đè toàn bộ)
                if post_id in real_metrics_map:
                    batch_data = real_metrics_map[post_id]
                    views = batch_data.get('view_count', 0)
                    likes = batch_data.get('like_count', likes)
                    comments = batch_data.get('comment_count', comments)
                    shares = batch_data.get('share_count', shares)
                    if isinstance(raw_json, dict):
                        raw_json['_metrics'] = batch_data.get('raw_json', {})

                # video_url đã được _normalize_post bóc tách đúng từ media{source}
                # Fallback: lấy lại từ raw_data nếu normalize bỏ sót
                video_url = p.get('video_url') or p.get('download_url') or ''
                if not video_url:
                    raw_original = p.get('raw_data') or {}
                    for att in raw_original.get('attachments', {}).get('data', []):
                        src = att.get('media', {}).get('source', '')
                        if src:
                            video_url = src
                            break

                # Parse published_at từ created_time (ISO string) hoặc timestamp (int)
                published_at = self._parse_published_at(p)

                defaults = {
                    'caption': p.get('description') or p.get('title') or '',
                    'published_at': published_at,
                    'permalink_url': p.get('url') or '',
                    'thumbnail_url': p.get('thumbnail_url') or p.get('thumbnail') or '',
                    'video_url': video_url,
                    'view_count': views,
                    'like_count': likes,
                    'comment_count': comments,
                    'share_count': shares,
                    'raw_data': raw_json,
                }

                obj, was_created = OwnedVideoContent.objects.update_or_create(
                    post_id=str(post_id),
                    defaults={**defaults, 'managed_page': page_obj}
                )

                if was_created:
                    created_count += 1
                else:
                    updated_count += 1

                thumbnail = defaults.get('thumbnail_url') or ''
                if thumbnail and (was_created or not obj.thumbnail_drive_url):
                    from ..tasks import upload_thumbnail_to_drive_task
                    upload_thumbnail_to_drive_task.delay(
                        model='facebook_owned_video',
                        object_id=obj.id,
                        cdn_url=thumbnail,
                        filename=f'fb-owned-{post_id}.jpg',
                    )

            # Đánh dấu trang đã được sync thành công
            page_obj.mark_synced()

            logger.info(f"📊 Kết quả Page {page_obj.name}: Tạo mới {created_count} video, Cập nhật {updated_count} video.")
            return {
                'page_id': page_obj.page_id,
                'managed_created': bool(created),
                'created': created_count,
                'updated': updated_count,
            }

        except Exception as e:
            logger.error(f"❌ Lỗi khi đồng bộ Page {page_id}: {str(e)}", exc_info=True)
            raise

        finally:
            # Khôi phục lại token hệ thống
            if page_access_token:
                self.graph.access_token = original_token

    def backfill_page(self, page_id: str, max_total: int = 300) -> dict:
        """Giai đoạn 1: Cào sâu lịch sử một page (lần theo phân trang).

        Chỉ chạy 1 lần cho page chưa backfill. Sau khi xong đánh dấu is_backfilled=True.
        """
        import time

        page_obj = ManagedFacebookPage.objects.get(page_id=str(page_id))
        page_access_token = page_obj.get_decrypted_token()
        if not page_access_token:
            raise ValueError(f"Không giải mã được token cho page {page_id}")

        original_token = getattr(self.graph, 'access_token', None)
        self.graph.access_token = page_access_token

        try:
            logger.info(f"═══ [BACKFILL] Bắt đầu cào lịch sử: {page_obj.name} (max={max_total}) ═══")

            raw_posts = self.graph.get_page_posts_deep(
                page_id=page_id,
                max_total=max_total,
                page_size=100,
                cooldown=1.0,
                access_token=page_access_token,
            )

            video_posts = [p for p in raw_posts if p.get('is_video')]
            logger.info(f"📹 Tìm thấy {len(video_posts)} video trong {len(raw_posts)} bài")

            if not video_posts:
                page_obj.is_backfilled = True
                page_obj.save(update_fields=['is_backfilled', 'updated_at'])
                return {'page_id': page_id, 'created': 0, 'updated': 0, 'total_scanned': len(raw_posts)}

            # Batch lấy metrics (chia nhóm 50 IDs mỗi lần)
            video_ids = [p.get('id') for p in video_posts if p.get('id')]
            real_metrics_map = {}
            batch_size = 50
            for i in range(0, len(video_ids), batch_size):
                chunk = video_ids[i:i + batch_size]
                chunk_metrics = self.graph.update_video_views_batch(chunk)
                real_metrics_map.update(chunk_metrics)
                if i + batch_size < len(video_ids):
                    time.sleep(1)

            created_count = 0
            updated_count = 0

            for p in video_posts:
                post_id = p.get('id')
                if not post_id:
                    continue

                likes = p.get('like_count', 0)
                comments = p.get('comment_count', 0)
                shares = p.get('share_count', 0)
                views = 0
                raw_json = p.get('raw_data') or p

                if post_id in real_metrics_map:
                    batch_data = real_metrics_map[post_id]
                    views = batch_data.get('view_count', 0)
                    likes = batch_data.get('like_count', likes)
                    comments = batch_data.get('comment_count', comments)
                    shares = batch_data.get('share_count', shares)
                    if isinstance(raw_json, dict):
                        raw_json['_metrics'] = batch_data.get('raw_json', {})

                video_url = p.get('video_url') or p.get('download_url') or ''
                if not video_url:
                    raw_original = p.get('raw_data') or {}
                    for att in raw_original.get('attachments', {}).get('data', []):
                        src = att.get('media', {}).get('source', '')
                        if src:
                            video_url = src
                            break

                published_at = self._parse_published_at(p)

                defaults = {
                    'caption': p.get('description') or p.get('title') or '',
                    'published_at': published_at,
                    'permalink_url': p.get('url') or '',
                    'thumbnail_url': p.get('thumbnail_url') or p.get('thumbnail') or '',
                    'video_url': video_url,
                    'view_count': views,
                    'like_count': likes,
                    'comment_count': comments,
                    'share_count': shares,
                    'raw_data': raw_json,
                }

                obj, was_created = OwnedVideoContent.objects.update_or_create(
                    post_id=str(post_id),
                    defaults={**defaults, 'managed_page': page_obj},
                )
                if was_created:
                    created_count += 1
                else:
                    updated_count += 1

                thumbnail = defaults.get('thumbnail_url') or ''
                if thumbnail and (was_created or not obj.thumbnail_drive_url):
                    from ..tasks import upload_thumbnail_to_drive_task
                    upload_thumbnail_to_drive_task.delay(
                        model='facebook_owned_video',
                        object_id=obj.id,
                        cdn_url=thumbnail,
                        filename=f'fb-owned-{post_id}.jpg',
                    )

            page_obj.is_backfilled = True
            page_obj.last_scraped_at = timezone.now()
            page_obj.save(update_fields=['is_backfilled', 'last_scraped_at', 'updated_at'])

            logger.info(f"═══ [BACKFILL] {page_obj.name}: +{created_count} mới, ~{updated_count} cập nhật ═══")
            return {
                'page_id': page_id,
                'created': created_count,
                'updated': updated_count,
                'total_scanned': len(raw_posts),
            }

        finally:
            self.graph.access_token = original_token

    def refresh_recent_metrics(self, days: int = 7) -> dict:
        """Giai đoạn 3: Cập nhật metrics cho video đăng trong N ngày gần đây.

        Quét DB tìm video có published_at trong 7 ngày gần nhất,
        gom ID theo page, gọi batch API cập nhật views/likes/comments/shares.
        """
        import time
        cutoff = timezone.now() - timezone.timedelta(days=days)
        recent_videos = OwnedVideoContent.objects.filter(
            published_at__gte=cutoff,
            managed_page__is_active=True,
        ).select_related('managed_page')

        if not recent_videos.exists():
            logger.info(f"[METRICS] Không có video nào trong {days} ngày gần đây")
            return {'updated': 0, 'total': 0}

        # Gom video theo page_id để dùng đúng token
        page_groups: dict = {}
        for v in recent_videos:
            pid = v.managed_page.page_id
            if pid not in page_groups:
                page_groups[pid] = {'page': v.managed_page, 'videos': []}
            page_groups[pid]['videos'].append(v)

        total_updated = 0
        original_token = getattr(self.graph, 'access_token', None)

        for pid, group in page_groups.items():
            page = group['page']
            videos = group['videos']
            token = page.get_decrypted_token()
            if not token:
                logger.warning(f"⚠️ Không giải mã được token cho page {page.name}")
                continue

            self.graph.access_token = token

            # Chia batch 50 IDs
            video_ids = [v.post_id for v in videos]
            for i in range(0, len(video_ids), 50):
                chunk = video_ids[i:i + 50]
                metrics_map = self.graph.update_video_views_batch(chunk)

                for v in videos:
                    if v.post_id in metrics_map:
                        m = metrics_map[v.post_id]
                        v.view_count = m.get('view_count', v.view_count)
                        v.like_count = m.get('like_count', v.like_count)
                        v.comment_count = m.get('comment_count', v.comment_count)
                        v.share_count = m.get('share_count', v.share_count)
                        v.save(update_fields=[
                            'view_count', 'like_count', 'comment_count',
                            'share_count', 'updated_at',
                        ])
                        total_updated += 1

                if i + 50 < len(video_ids):
                    time.sleep(1)

            logger.info(f"📊 [METRICS] {page.name}: cập nhật {len(video_ids)} video")
            time.sleep(1.5)

        self.graph.access_token = original_token
        logger.info(f"✅ [METRICS] Tổng: {total_updated} video cập nhật metrics")
        return {'updated': total_updated, 'total': recent_videos.count()}

    def sync_all_managed(self, max_posts: int = 50) -> None:
        """Sync all active ManagedFacebookPage entries."""
        pages = ManagedFacebookPage.objects.filter(is_active=True)
        logger.info(f"Found {pages.count()} managed pages to sync")
        for page in pages:
            try:
                # Lấy token đã giải mã từ model
                decrypted_token = page.get_decrypted_token()
                if not decrypted_token:
                    logger.error(f"Failed to decrypt token for page {page.page_id}")
                    continue
                self.sync_page(page.page_id, page_access_token=decrypted_token, max_posts=max_posts)
            except Exception as e:
                logger.exception(f"Failed to sync page {page.page_id}: {e}")


# Convenience functions
def sync_page_by_id(page_id: str, page_access_token: Optional[str] = None, max_posts: int = 50):
    f = FacebookFetcher()
    return f.sync_page(page_id, page_access_token=page_access_token, max_posts=max_posts)


def refresh_pages_metrics():
    """Convenience function for background workers to refresh all pages metrics."""
    f = FacebookFetcher()
    return f.refresh_all_pages_metrics()
