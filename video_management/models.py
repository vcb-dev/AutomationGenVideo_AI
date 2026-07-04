"""
Database models for video scraping and management.

This module defines the core data models for tracking scraped videos,
search history, and platform-specific metadata.
"""

from django.db import models
from django.utils import timezone
from django.core.validators import MinValueValidator
from typing import Optional
import json

try:
    from video_management.utils.encryption import TokenEncryption
except ImportError:
    from .utils.encryption import TokenEncryption


class Platform(models.TextChoices):
    """Supported social media platforms."""
    TIKTOK = 'tiktok', 'TikTok'
    DOUYIN = 'douyin', 'Douyin'
    INSTAGRAM = 'instagram', 'Instagram'
    FACEBOOK = 'facebook', 'Facebook'
    XIAOHONGSHU = 'xiaohongshu', 'Xiaohongshu'
    YOUTUBE = 'youtube', 'YouTube'


class SearchStatus(models.TextChoices):
    """Status of search operations."""
    PENDING = 'pending', 'Pending'
    PROCESSING = 'processing', 'Processing'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'
    CACHED = 'cached', 'Cached'


class BaseModel(models.Model):
    """Abstract base model with common fields."""
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ManagedFacebookPage(BaseModel):
    """
    Quản lý các Fanpage chính chủ do người dùng cấp quyền qua Facebook Login App.
    
    Lưu trữ Page Access Token (mã hóa) và các chỉ số cốt lõi để phục vụ tính năng 
    đồng bộ báo cáo (Insights) và tự động đăng bài (Auto-post).
    """
    # 1. Định danh Fanpage (Who are you?)
    page_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="ID cứng của Fanpage do Facebook cấp"
    )
    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Tên hiển thị của Fanpage"
    )
    username = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Facebook Page Username (Handle dạng chữ nếu có)"
    )
    category = models.CharField(
        max_length=255,
        blank=True,
        help_text="Danh mục chính của Trang"
    )
    avatar_url = models.TextField(
        blank=True,
        null=True,
        help_text="URL ảnh đại diện lấy từ Facebook"
    )
    avatar_drive_url = models.TextField(blank=True, null=True)

    # 2. Quyền hạn & Mật mã (Credentials & Permissions) - Token được mã hóa trước lưu
    page_access_token = models.TextField(
        default='',
        help_text="[ENCRYPTED] Page Access Token dài hạn (mã hóa trước khi lưu DB)"
    )
    _token_decrypted = None  # Internal cache để tránh decrypt nhiều lần
    
    tasks = models.JSONField(
        default=list,
        blank=True,
        help_text="Danh sách các quyền cụ thể user có trên Page này (e.g., ANALYZE, CREATE_CONTENT)"
    )

    # 3. Snapshot Chỉ số cơ bản (Gắn thẳng vào đây để FE render nhanh danh sách trang)
    followers_count = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Số lượng người theo dõi hiện tại"
    )
    likes_count = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Số lượng lượt thích Trang hiện tại"
    )

    # 4. Trạng thái & Điều phối (Control & Syncing)
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Bật/Tắt đồng bộ dữ liệu tự động cho Page này"
    )
    last_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Thời điểm cuối cùng hệ thống kéo Insights/Metrics của Page này về"
    )
    last_scraped_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Thời điểm cuối cùng Cron Job cào video thành công"
    )
    is_scraping = models.BooleanField(
        default=False,
        help_text="Flag khóa: True khi đang có worker cào page này (tránh trùng lặp)"
    )
    is_backfilled = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True khi đã cào hết lịch sử (Giai đoạn 1). Cron delta sync chỉ quét page đã backfill."
    )
    scrape_error = models.TextField(
        blank=True,
        null=True,
        help_text="Lỗi cuối cùng nếu lần cào gần nhất thất bại"
    )

    # 5. Lưu trữ thô phòng vệ (Data Insurance)
    raw_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Toàn bộ cục dữ liệu gốc trả về từ Graph API lúc phân quyền"
    )

    class Meta:
        managed = False
        verbose_name = "Managed Facebook Page"
        verbose_name_plural = "Managed Facebook Pages"
        ordering = ['-last_synced_at', 'name']
        indexes = [
            models.Index(fields=['is_active', 'name']),
            models.Index(fields=['is_active', 'last_synced_at']),
        ]

    def __str__(self) -> str:
        return f"Managed: {self.name} (ID: {self.page_id})"
    
    def save(self, *args, **kwargs):
        """Override save to auto-encrypt token before storing in DB."""
        # Nếu token chưa được mã hóa (plain text), thì mã hóa nó trước save
        if self.page_access_token and not self.page_access_token.startswith('gAAAAAB'):
            try:
                self.page_access_token = TokenEncryption.encrypt(self.page_access_token)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Token encryption failed: {str(e)}")
                raise
        super().save(*args, **kwargs)
    
    def get_decrypted_token(self) -> str:
        # Nếu đã từng decrypt trong phiên chạy này, trả về luôn để tiết kiệm CPU
        if self._token_decrypted:
            return self._token_decrypted
        """Get decrypted access token."""
        if not self.page_access_token:
            return ''
        
        # Nếu token này đã là plain text (not encrypted), return as-is
        if not self.page_access_token.startswith('gAAAAAB'):
            self._token_decrypted = self.page_access_token
            return self.page_access_token
        
        # Decrypt encrypted token
        try:
            self._token_decrypted = TokenEncryption.decrypt(self.page_access_token)
            return TokenEncryption.decrypt(self.page_access_token)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Token decryption failed: {str(e)}")
            return ''

    def mark_synced(self) -> None:
        """Cập nhật thời gian đồng bộ thành công mà không gây Lock toàn bộ bảng."""
        self.last_synced_at = timezone.now()
        self.save(update_fields=['last_synced_at', 'updated_at'])

    @property
    def has_analyze_permission(self) -> bool:
        """Kiểm tra nhanh xem token này có quyền đọc số liệu báo cáo không."""
        return 'ANALYZE' in self.tasks

    @property
    def has_publish_permission(self) -> bool:
        """Kiểm tra nhanh xem token này có quyền đăng bài hộ không."""
        return 'CREATE_CONTENT' in self.tasks
    
class OwnedVideoContent(BaseModel):
    """
    Lưu trữ video/Reels chính chủ.
    Đã tối ưu hóa dựa trên cấu trúc Graph API thực tế của Facebook.
    """
    managed_page = models.ForeignKey(
        'ManagedFacebookPage',
        on_delete=models.CASCADE,
        related_name='videos'
    )
    
    # Khớp với "id" của FB (964204583450876_122116652625288597)
    post_id = models.CharField(max_length=255, unique=True, db_index=True)
    
    # Khớp với "message" (Vì Reels/Post FB không có tiêu đề riêng, chỉ có nội dung chữ)
    caption = models.TextField(blank=True, help_text="Nội dung bài viết/Hashtag")
    
    # Khớp với "created_time"
    published_at = models.DateTimeField(db_index=True)
    
    # Khớp với "permalink_url" (Link xem trực tiếp trên FB)
    permalink_url = models.URLField(max_length=500, blank=True, null=True)
    
    # Khớp với "full_picture" (Dùng làm ảnh preview trên Dashboard)
    thumbnail_url = models.TextField(blank=True, null=True)
    thumbnail_drive_url = models.TextField(blank=True, null=True)
    
    # Khớp với "attachments.data[0].media.source" (Link file .mp4 gốc)
    # BẮT BUỘC dùng TextField vì link CDN của Facebook cực kỳ dài
    video_url = models.TextField(blank=True, null=True)

    # --- Các trường chỉ số (Metrics) sẽ được cập nhật ở bước sau ---
    view_count = models.BigIntegerField(default=0, db_index=True)
    like_count = models.IntegerField(default=0)
    comment_count = models.IntegerField(default=0)
    share_count = models.IntegerField(default=0)
    
    # Chỉ số nội bộ để phân tích Ads / Sản xuất
    reach_count = models.IntegerField(default=0)
    link_clicks = models.IntegerField(default=0)

    raw_data = models.JSONField(default=dict, blank=True)
    last_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        verbose_name = "Owned Video Content"
        verbose_name_plural = "Owned Video Contents"
        ordering = ['-published_at']
        indexes = [
            models.Index(fields=['managed_page', '-view_count']),
        ]

    def __str__(self) -> str:
        return f"Owned: {self.post_id} (Views: {self.view_count})"

class SearchHistory(BaseModel):
    """
    Track all search operations across platforms.
    
    This model stores search queries, their status, and results for caching
    and analytics purposes.
    """
    platform = models.CharField(
        max_length=20,
        choices=Platform.choices,
        db_index=True,
        help_text="Social media platform"
    )
    keyword = models.CharField(
        max_length=500,
        db_index=True,
        help_text="Search keyword or hashtag"
    )
    status = models.CharField(
        max_length=20,
        choices=SearchStatus.choices,
        default=SearchStatus.PENDING,
        db_index=True
    )
    
    # Filter parameters
    min_likes = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Minimum likes filter"
    )
    min_views = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Minimum views filter"
    )
    max_results = models.IntegerField(
        default=20,
        validators=[MinValueValidator(1)],
        help_text="Maximum number of results"
    )
    search_mode = models.CharField(
        max_length=20,
        default='hashtag',
        blank=True,
        db_index=True,
        help_text="Search mode: keyword (caption) or hashtag"
    )
    
    # Results
    results_count = models.IntegerField(
        default=0,
        help_text="Number of results found"
    )
    raw_results = models.JSONField(
        default=list,
        blank=True,
        help_text="Raw API response data"
    )
    
    # Metadata
    task_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        db_index=True,
        help_text="Celery task ID for async operations"
    )
    error_message = models.TextField(
        blank=True,
        null=True,
        help_text="Error message if search failed"
    )
    execution_time = models.FloatField(
        default=0.0,
        help_text="Execution time in seconds"
    )
    
    # Cache control
    expires_at = models.DateTimeField(
        db_index=True,
        help_text="When this cache entry expires"
    )
    
    class Meta:
        verbose_name = "Search History"
        verbose_name_plural = "Search Histories"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['platform', 'keyword', '-created_at']),
            models.Index(fields=['status', '-created_at']),
        ]
    
    def __str__(self) -> str:
        return f"{self.platform}: {self.keyword} ({self.status})"
    
    def is_expired(self) -> bool:
        """Check if cache entry is expired."""
        return timezone.now() > self.expires_at
    
    def mark_completed(self, results: list, execution_time: float = 0.0) -> None:
        """Mark search as completed with results."""
        import json
        from datetime import datetime
        
        # Convert datetime objects to strings for JSON serialization
        def serialize_datetime(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            return obj
        
        # Deep copy and serialize results
        serialized_results = json.loads(
            json.dumps(results, default=serialize_datetime)
        )
        
        self.status = SearchStatus.COMPLETED
        self.raw_results = serialized_results
        self.results_count = len(results)
        self.execution_time = execution_time
        self.save()
    
    def mark_failed(self, error: str) -> None:
        """Mark search as failed with error message."""
        self.status = SearchStatus.FAILED
        self.error_message = error
        self.save()


class ScrapedVideo(BaseModel):
    """
    Store scraped video metadata from various platforms.
    
    This model normalizes video data across different platforms for
    consistent storage and retrieval.
    """
    platform = models.CharField(
        max_length=20,
        choices=Platform.choices,
        db_index=True
    )
    video_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Platform-specific video ID"
    )
    
    # Video metadata
    title = models.TextField(
        blank=True,
        help_text="Video title or caption"
    )
    description = models.TextField(
        blank=True,
        help_text="Video description"
    )
    author_username = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Content creator username"
    )
    author_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Content creator display name"
    )
    
    # Engagement metrics
    likes_count = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        db_index=True
    )
    views_count = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        db_index=True
    )
    comments_count = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0)]
    )
    shares_count = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0)]
    )
    
    # URLs
    video_url = models.URLField(
        max_length=1000,
        help_text="Direct video URL"
    )
    download_url = models.URLField(
        max_length=1000,
        blank=True,
        help_text="Download URL (if available)"
    )
    thumbnail_url = models.URLField(
        max_length=1000,
        blank=True,
        help_text="Thumbnail image URL"
    )
    
    # Timestamps
    published_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When video was published on platform"
    )
    
    # Additional data
    hashtags = models.JSONField(
        default=list,
        blank=True,
        help_text="List of hashtags"
    )
    
    
    # DEPRECATED: Duplicate detection feature has been removed
    # This field is kept for backward compatibility only
    # TODO: Remove in future migration
    feature_vector = models.BinaryField(
        null=True, 
        blank=True,
        help_text="[DEPRECATED] Extracted feature vector for AI comparison"
    )
    duration = models.FloatField(
        default=0,
        help_text="Video duration in seconds"
    )
    music_info = models.JSONField(
        default=dict,
        blank=True,
        help_text="Music/audio information"
    )
    raw_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Complete raw data from platform"
    )
    
    # Relationships
    search_history = models.ForeignKey(
        SearchHistory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='videos',
        help_text="Search that found this video"
    )
    
    class Meta:
        verbose_name = "Scraped Video"
        verbose_name_plural = "Scraped Videos"
        ordering = ['-likes_count', '-views_count']
        indexes = [
            models.Index(fields=['platform', '-likes_count']),
            models.Index(fields=['platform', '-views_count']),
            models.Index(fields=['author_username', '-created_at']),
        ]
    
    def __str__(self) -> str:
        return f"{self.platform}: {self.video_id} by @{self.author_username}"
    
    @property
    def engagement_rate(self) -> float:
        """Calculate engagement rate (likes + comments / views)."""
        if self.views_count == 0:
            return 0.0
        return ((self.likes_count + self.comments_count) / self.views_count) * 100


class TrackedChannel(BaseModel):
    """
    Track specific channels/accounts across platforms.
    
    Used for monitoring specific content creators and their content.
    """
    platform = models.CharField(
        max_length=20,
        choices=Platform.choices,
        db_index=True
    )
    channel_id = models.CharField(
        max_length=255,
        help_text="Platform-specific channel/user ID"
    )
    username = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Channel username"
    )
    display_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Channel display name"
    )
    
    # Tracking settings
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether to actively track this channel"
    )
    check_interval_minutes = models.IntegerField(
        default=60,
        validators=[MinValueValidator(5)],
        help_text="How often to check for new content (minutes)"
    )
    
    # Filters
    min_likes_threshold = models.IntegerField(
        default=1000,
        validators=[MinValueValidator(0)],
        help_text="Only track videos with at least this many likes"
    )
    
    # Metadata
    last_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time this channel was checked"
    )
    follower_count = models.BigIntegerField(
        default=0,
        help_text="Number of followers/subscribers"
    )
    
    class Meta:
        verbose_name = "Tracked Channel"
        verbose_name_plural = "Tracked Channels"
        ordering = ['-created_at']
        unique_together = [['platform', 'channel_id']]
        indexes = [
            models.Index(fields=['platform', 'is_active']),
            models.Index(fields=['is_active', 'last_checked_at']),
        ]
    
    def __str__(self) -> str:
        return f"{self.platform}: @{self.username}"
    
    def mark_checked(self) -> None:
        """Update last checked timestamp."""
        self.last_checked_at = timezone.now()
        self.save(update_fields=['last_checked_at', 'updated_at'])


class VideoCollection(BaseModel):
    """
    User-created collections/labels for organizing videos.
    
    Allows users to create custom collections (e.g., "Viral Dance", "Food Videos")
    and add videos to them for better organization.
    """
    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Collection name/label"
    )
    description = models.TextField(
        blank=True,
        help_text="Collection description"
    )
    color = models.CharField(
        max_length=7,
        default='#3B82F6',
        help_text="Color for UI display (hex format)"
    )
    
    # Relationships
    videos = models.ManyToManyField(
        ScrapedVideo,
        through='CollectionVideo',
        related_name='collections',
        help_text="Videos in this collection"
    )
    
    class Meta:
        verbose_name = "Video Collection"
        verbose_name_plural = "Video Collections"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['name', '-created_at']),
        ]
    
    def __str__(self) -> str:
        return self.name
    
    @property
    def video_count(self) -> int:
        """Get number of videos in collection."""
        return self.videos.count()


class CollectionVideo(BaseModel):
    """
    Through model for VideoCollection and ScrapedVideo many-to-many relationship.
    
    Stores additional metadata about when and why a video was added to a collection.
    """
    collection = models.ForeignKey(
        VideoCollection,
        on_delete=models.CASCADE,
        related_name='collection_videos'
    )
    video = models.ForeignKey(
        ScrapedVideo,
        on_delete=models.CASCADE,
        related_name='video_collections'
    )
    
    # Optional metadata
    notes = models.TextField(
        blank=True,
        help_text="User notes about why this video was added"
    )
    order = models.IntegerField(
        default=0,
        help_text="Display order within collection"
    )
    
    class Meta:
        verbose_name = "Collection Video"
        verbose_name_plural = "Collection Videos"
        ordering = ['order', '-created_at']
        unique_together = [['collection', 'video']]
        indexes = [
            models.Index(fields=['collection', 'order']),
        ]
    
    def __str__(self) -> str:
        return f"{self.collection.name}: {self.video.video_id}"


class FacebookPageCache(BaseModel):
    """
    Cache Facebook page metadata to reduce Apify API calls.
    
    Stores page information (followers, avatar, etc.) with a 24-hour TTL.
    This significantly reduces quota usage since page info rarely changes.
    """
    # Page identification
    username = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Facebook page username or ID"
    )
    
    # Cached page information
    page_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Display name of the page"
    )
    avatar_url = models.URLField(
        max_length=1000,
        blank=True,
        help_text="Profile picture URL"
    )
    followers_count = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Number of followers"
    )
    likes_count = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Number of page likes"
    )
    
    # Additional metadata
    page_description = models.TextField(
        blank=True,
        help_text="Page description/bio"
    )
    page_category = models.CharField(
        max_length=255,
        blank=True,
        help_text="Page category"
    )
    verified = models.BooleanField(
        default=False,
        help_text="Whether page is verified"
    )
    
    # Raw data from Apify
    raw_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Complete raw data from Apify"
    )
    
    # Cache control
    expires_at = models.DateTimeField(
        db_index=True,
        help_text="When this cache entry expires (24h TTL)"
    )
    last_fetched_at = models.DateTimeField(
        auto_now=True,
        help_text="Last time data was fetched from external API"
    )
    
    class Meta:
        verbose_name = "Facebook Page Cache"
        verbose_name_plural = "Facebook Page Caches"
        ordering = ['-last_fetched_at']
        indexes = [
            models.Index(fields=['username', 'expires_at']),
        ]
    
    def __str__(self) -> str:
        return f"@{self.username} (expires: {self.expires_at})"
    
    def is_expired(self) -> bool:
        """Check if cache entry is expired."""
        return timezone.now() > self.expires_at
    
    def refresh_expiry(self) -> None:
        """Extend cache expiry by 24 hours from now."""
        from datetime import timedelta
        self.expires_at = timezone.now() + timedelta(hours=24)
        self.save(update_fields=['expires_at', 'last_fetched_at'])
    
    @classmethod
    def get_or_fetch(cls, username: str, fetch_callback=None):
        """
        Get cached page info or fetch new data if expired.
        
        Args:
            username: Facebook page username/ID
            fetch_callback: Function to call if cache is expired or missing.
                           Should return dict with page info.
        
        Returns:
            dict: Page information
        """
        from datetime import timedelta
        
        # Try to get from cache
        try:
            cache = cls.objects.get(username=username)
            if not cache.is_expired():
                # Cache hit - return cached data
                return {
                    'username': cache.username,
                    'name': cache.page_name,
                    'display_name': cache.page_name,
                    'avatar_url': cache.avatar_url,
                    'followers': cache.followers_count,
                    'likes': cache.likes_count,
                    'description': cache.page_description,
                    'category': cache.page_category,
                    'verified': cache.verified,
                    'source': 'cache',
                    'cached_at': cache.last_fetched_at.isoformat()
                }
            else:
                # Cache expired - delete and refetch
                cache.delete()
        except cls.DoesNotExist:
            pass
        
        # Cache miss or expired - fetch new data
        if fetch_callback:
            page_info = fetch_callback(username)
            
            # Extract followers/likes with multiple key fallbacks
            # Apify facebook-pages-scraper uses 'followers'/'likes' OR 'followersCount'/'likesCount'
            def _extract_int(d, *keys):
                for k in keys:
                    v = d.get(k)
                    if v is not None:
                        try:
                            return int(str(v).replace(',', '').replace('.', '').strip())
                        except (ValueError, AttributeError):
                            continue
                return 0
            
            followers_val = _extract_int(page_info, 'followers', 'followersCount', 'follower_count', 'fans')
            likes_val = _extract_int(page_info, 'likes', 'likesCount', 'like_count', 'page_likes')
            avatar_val = (page_info.get('profilePic') or page_info.get('image') or 
                          page_info.get('avatar_url') or page_info.get('picture') or '')
            page_name_val = (page_info.get('name') or page_info.get('title') or 
                             page_info.get('display_name') or username)
            
            # Store in cache
            try:
                cache = cls.objects.create(
                    username=username,
                    page_name=page_name_val,
                    avatar_url=avatar_val,
                    followers_count=followers_val,
                    likes_count=likes_val,
                    page_description=page_info.get('description', ''),
                    page_category=page_info.get('category', ''),
                    verified=page_info.get('verified', False),
                    raw_data=page_info,
                    expires_at=timezone.now() + timedelta(hours=24)
                )
            except Exception:
                # If create fails (e.g., duplicate), update existing
                cls.objects.update_or_create(
                    username=username,
                    defaults={
                        'page_name': page_name_val,
                        'avatar_url': avatar_val,
                        'followers_count': followers_val,
                        'likes_count': likes_val,
                        'page_description': page_info.get('description', ''),
                        'page_category': page_info.get('category', ''),
                        'verified': page_info.get('verified', False),
                        'raw_data': page_info,
                        'expires_at': timezone.now() + timedelta(hours=24)
                    }
                )
            
            # Normalize page_info keys for consistency downstream
            page_info['followers'] = followers_val
            page_info['likes'] = likes_val
            page_info['avatar_url'] = avatar_val
            page_info['name'] = page_name_val
            page_info['source'] = 'fresh_fetch'
            return page_info
        
        # No callback provided - return empty data
        return {
            'username': username,
            'name': username,
            'display_name': username,
            'followers': 0,
            'likes': 0,
            'source': 'fallback'
        }


class ChannelAnalysis(BaseModel):
    """
    Store previously generated AI insights and metrics for a channel,
    to avoid re-running expensive Apify and Gemini calls.
    """
    platform = models.CharField(max_length=20, choices=Platform.choices, db_index=True)
    username = models.CharField(max_length=255, db_index=True)
    max_posts = models.IntegerField(default=30)
    
    insights = models.JSONField(default=dict, blank=True, help_text="Stored Gemini insights JSON")
    metrics = models.JSONField(default=dict, blank=True, help_text="Stored metrics/charts JSON")
    
    class Meta:
        verbose_name = "Channel Analysis"
        verbose_name_plural = "Channel Analyses"
        # Unique per platform+username so we just update the latest analysis
        unique_together = [['platform', 'username']]
        indexes = [
            models.Index(fields=['platform', 'username']),
        ]
        
    def __str__(self):
        return f"{self.platform}: {self.username} ({self.updated_at.strftime('%Y-%m-%d')})"

class TikTokUserCache(BaseModel):
    """
    Stores TikTok user profile information with 24h TTL.
    """
    username = models.CharField(max_length=255, unique=True, db_index=True)
    display_name = models.CharField(max_length=255, blank=True)
    avatar_url = models.TextField(blank=True, null=True)
    followers_count = models.BigIntegerField(default=0)
    likes_count = models.BigIntegerField(default=0) # Total hearts
    videos_count = models.IntegerField(default=0)
    
    raw_data = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(db_index=True, help_text="Cache expiry 24h")
    last_fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['username']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"{self.username} (Exp: {self.expires_at})"

    def is_expired(self):
        return timezone.now() > self.expires_at

    @classmethod
    def get_or_fetch(cls, username: str, fetch_callback=None):
        from datetime import timedelta
        # Cache logic similar to FacebookPageCache
        try:
            cache = cls.objects.get(username=username)
            if not cache.is_expired():
                return {
                    'username': cache.username,
                    'display_name': cache.display_name,
                    'avatar_url': cache.avatar_url,
                    'follower_count': cache.followers_count,
                    'total_likes': cache.likes_count,
                    'total_videos': cache.videos_count,
                    'source': 'cache'
                }
        except cls.DoesNotExist:
            pass
            
        if fetch_callback:
            data = fetch_callback(username)
            if data:
                expires = timezone.now() + timedelta(hours=24)
                cls.objects.update_or_create(
                    username=username,
                    defaults={
                        'display_name': data.get('display_name', username),
                        'avatar_url': data.get('avatar_url', ''),
                        'followers_count': data.get('follower_count', 0),
                        'likes_count': data.get('total_likes', 0),
                        'videos_count': data.get('total_videos', 0),
                        'raw_data': data,
                        'expires_at': expires
                    }
                )
                data['source'] = 'fresh'
                return data
        return None

class Voice(BaseModel):
    """
    Store custom and system voices for TTS/Lipsync.
    """
    name = models.CharField(
        max_length=255,
        help_text="Friendly name of the voice"
    )
    voice_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Provider-specific voice ID (e.g., HeyGen ID)"
    )
    provider = models.CharField(
        max_length=50,
        default='heygen',
        help_text="Voice provider (heygen, elevenlabs, etc.)"
    )
    is_cloned = models.BooleanField(
        default=False,
        help_text="Whether this is a cloned voice from a user"
    )
    is_system = models.BooleanField(
        default=False,
        help_text="Whether this is a default system voice"
    )
    language = models.CharField(
        max_length=10,
        default='vi',
        help_text="Language code (vi, en, etc.)"
    )
    gender = models.CharField(
        max_length=20,
        choices=[('male', 'Male'), ('female', 'Female')],
        blank=True,
        null=True
    )
    sample_audio_url = models.URLField(
        blank=True,
        null=True,
        help_text="URL to sample audio of this voice"
    )
    
    # Optional: Link to original scraped video if cloned from KOC
    source_video = models.ForeignKey(
        ScrapedVideo,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derived_voices',
        help_text="Source video this voice was cloned from"
    )

    class Meta:
        verbose_name = "Voice"
        verbose_name_plural = "Voices"
        ordering = ['-is_system', 'name']
        indexes = [
            models.Index(fields=['provider', 'is_system']),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.provider})"


class ContentType(models.TextChoices):
    """Content types based on marketing strategy."""
    A1_TRAFFIC = 'A1', 'Traffic (Viral Content)'
    A2_KNOWLEDGE = 'A2', 'Knowledge (Educational)'
    A3_CREDIBILITY = 'A3', 'Credibility (Trust Building)'
    A4_CONVERSION = 'A4', 'Conversion (Sales)'
    A5_COMBINED = 'A5', 'Combined (All Types)'


class GeneratedContent(BaseModel):
    """
    Store AI-generated content scripts based on viral videos.
    Each generated content is derived from a source video and follows
    a specific content type (A1-A5) strategy.
    """
    source_video = models.ForeignKey(
        ScrapedVideo,
        on_delete=models.CASCADE,
        related_name='generated_contents',
        help_text="Original viral video this content is based on"
    )
    
    content_type = models.CharField(
        max_length=2,
        choices=ContentType.choices,
        help_text="Content strategy type (A1-A5)"
    )
    
    # Generated script
    title = models.CharField(
        max_length=500,
        help_text="Generated video title"
    )
    
    script = models.TextField(
        help_text="Full generated script/content"
    )
    
    # Script structure breakdown
    hook = models.TextField(
        blank=True,
        help_text="Hook section (0-3s)"
    )
    
    problem = models.TextField(
        blank=True,
        help_text="Problem section"
    )
    
    solution = models.TextField(
        blank=True,
        help_text="Solution section"
    )
    
    cta = models.TextField(
        blank=True,
        help_text="Call-to-action section"
    )
    
    # Metadata
    estimated_duration = models.IntegerField(
        default=30,
        help_text="Estimated video duration in seconds"
    )
    
    word_count = models.IntegerField(
        default=0,
        help_text="Total word count"
    )
    
    # AI generation info
    ai_model = models.CharField(
        max_length=50,
        default='gpt-4',
        help_text="AI model used for generation"
    )
    
    prompt_used = models.TextField(
        blank=True,
        help_text="Prompt template used"
    )
    
    # Status
    is_approved = models.BooleanField(
        default=False,
        help_text="Whether this content is approved for use"
    )
    
    notes = models.TextField(
        blank=True,
        help_text="Additional notes or edits"
    )

    class Meta:
        verbose_name = "Generated Content"
        verbose_name_plural = "Generated Contents"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['content_type', 'is_approved']),
            models.Index(fields=['source_video', 'content_type']),
        ]

    def __str__(self) -> str:
        return f"{self.get_content_type_display()}: {self.title[:50]}"


class ProductList(BaseModel):
    """
    Store uploaded product catalog files.
    Users can upload Excel files containing their product inventory.
    """
    name = models.CharField(
        max_length=255,
        help_text="Name of the product list"
    )
    
    file_name = models.CharField(
        max_length=500,
        help_text="Original uploaded file name"
    )
    
    file_path = models.CharField(
        max_length=1000,
        help_text="Path to stored file"
    )
    
    total_products = models.IntegerField(
        default=0,
        help_text="Total number of products in this list"
    )
    
    description = models.TextField(
        blank=True,
        help_text="Optional description of this product list"
    )
    
    class Meta:
        verbose_name = "Product List"
        verbose_name_plural = "Product Lists"
        ordering = ['-created_at']
    
    def __str__(self) -> str:
        return f"{self.name} ({self.total_products} products)"


class Product(BaseModel):
    """
    Individual product from uploaded catalog.
    Parsed from Excel files and stored for content generation.
    """
    product_list = models.ForeignKey(
        ProductList,
        on_delete=models.CASCADE,
        related_name='products',
        help_text="Product list this product belongs to"
    )
    
    # Product information
    name = models.CharField(
        max_length=500,
        help_text="Product name"
    )
    
    category = models.CharField(
        max_length=255,
        blank=True,
        help_text="Product category (e.g., Nhẫn, Dây chuyền, Vòng tay)"
    )
    
    price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Product price"
    )
    
    description = models.TextField(
        blank=True,
        help_text="Product description"
    )
    
    highlights = models.TextField(
        blank=True,
        help_text="Key features/highlights"
    )
    
    sku = models.CharField(
        max_length=255,
        blank=True,
        help_text="Stock Keeping Unit / Product code"
    )
    
    # Additional metadata from Excel
    raw_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="All columns from Excel row"
    )
    
    class Meta:
        verbose_name = "Product"
        verbose_name_plural = "Products"
        ordering = ['product_list', 'category', 'name']
        indexes = [
            models.Index(fields=['product_list', 'category']),
            models.Index(fields=['name']),
        ]
    
    def __str__(self) -> str:
        return f"{self.name} ({self.category})"


class IndexedVideo(BaseModel):
    """
    NEW: Index of source videos from network storage for Smart Pre-processing.
    Stores only metadata (no file copying) for fast mix operations.
    """
    file_path = models.CharField(
        max_length=1000,
        db_index=True,
        help_text="Absolute path to video file (network or local)"
    )
    
    # Folder classification for mix-video (10-slot formula)
    folder_type = models.CharField(
        max_length=50,
        db_index=True,
        help_text="Folder type: Sản phẩm, HuyK, Chế tác, etc."
    )
    
    # Video metadata
    duration = models.FloatField(
        help_text="Video duration in seconds"
    )
    
    file_size = models.BigIntegerField(
        help_text="File size in bytes"
    )
    
    has_audio = models.BooleanField(
        default=False,
        help_text="Whether video has audio stream"
    )
    
    width = models.IntegerField(
        null=True,
        blank=True,
        help_text="Video width in pixels"
    )
    
    height = models.IntegerField(
        null=True,
        blank=True,
        help_text="Video height in pixels"
    )
    
    # Usage tracking
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Last time this video was used in a mix"
    )
    
    use_count = models.IntegerField(
        default=0,
        help_text="Number of times this video has been used"
    )
    
    # File validation
    modified_time = models.DateTimeField(
        help_text="File last modified timestamp"
    )
    
    is_available = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether file still exists and is accessible"
    )
    
    class Meta:
        verbose_name = "Indexed Video"
        verbose_name_plural = "Indexed Videos"
        ordering = ['-last_used_at']
        unique_together = [['file_path', 'folder_type']]
        indexes = [
            models.Index(fields=['folder_type', 'is_available', '-last_used_at']),
            models.Index(fields=['folder_type', 'duration']),
        ]
    
    def __str__(self) -> str:
        return f"{self.folder_type}: {self.file_path[-50:]} ({self.duration:.1f}s)"


class VideoClipCache(BaseModel):
    """
    NEW: Cache for pre-generated video clips (Lazy Loading).
    Stores short clips (5-10s) extracted from IndexedVideo for instant mixing.
    """
    source_video = models.ForeignKey(
        IndexedVideo,
        on_delete=models.CASCADE,
        related_name='clips',
        help_text="Source video this clip was extracted from"
    )
    
    clip_path = models.CharField(
        max_length=1000,
        unique=True,
        db_index=True,
        help_text="Path to cached clip file"
    )
    
    # Clip properties
    start_time = models.FloatField(
        help_text="Start time in source video (seconds)"
    )
    
    duration = models.FloatField(
        help_text="Clip duration in seconds"
    )
    
    file_size = models.BigIntegerField(
        help_text="Clip file size in bytes"
    )
    
    # Cache management
    last_accessed_at = models.DateTimeField(
        auto_now=True,
        db_index=True,
        help_text="Last time this clip was accessed (LRU)"
    )
    
    access_count = models.IntegerField(
        default=0,
        help_text="Number of times this clip has been used"
    )
    
    # Generation metadata
    generated_with_gpu = models.BooleanField(
        default=False,
        help_text="Whether this clip was generated with GPU acceleration"
    )
    
    generation_time = models.FloatField(
        default=0.0,
        help_text="Time taken to generate this clip (seconds)"
    )
    
    class Meta:
        verbose_name = "Video Clip Cache"
        verbose_name_plural = "Video Clip Caches"
        ordering = ['-last_accessed_at']
        indexes = [
            models.Index(fields=['source_video', '-last_accessed_at']),
            models.Index(fields=['-last_accessed_at']),  # For LRU cleanup
            models.Index(fields=['clip_path']),
        ]
    
    def __str__(self) -> str:
        return f"Clip from {self.source_video.folder_type} ({self.duration:.1f}s)"


class LocalVideoFile(BaseModel):
    """
    DEPRECATED: Use IndexedVideo and VideoClipCache instead.
    Cache local video file metadata for mix-video feature.
    Stores file path, duration, size, and modified time to avoid repeated ffprobe calls.
    Cache is invalidated when file size or mtime changes.
    """
    file_path = models.CharField(
        max_length=1000,
        unique=True,
        db_index=True,
        help_text="Absolute path to video file"
    )
    
    # File metadata for cache invalidation
    file_size = models.BigIntegerField(
        help_text="File size in bytes"
    )
    
    modified_time = models.DateTimeField(
        db_index=True,
        help_text="File last modified timestamp"
    )
    
    # Video metadata
    duration = models.FloatField(
        help_text="Video duration in seconds"
    )
    
    has_audio = models.BooleanField(
        default=False,
        help_text="Whether video has audio stream"
    )
    
    # Optional video properties
    width = models.IntegerField(
        null=True,
        blank=True,
        help_text="Video width in pixels"
    )
    
    height = models.IntegerField(
        null=True,
        blank=True,
        help_text="Video height in pixels"
    )
    
    # Cache metadata
    folder_name = models.CharField(
        max_length=500,
        db_index=True,
        blank=True,
        help_text="Parent folder name for quick filtering"
    )
    
    last_accessed_at = models.DateTimeField(
        auto_now=True,
        help_text="Last time this cache entry was accessed"
    )
    
    class Meta:
        verbose_name = "Local Video File Cache"
        verbose_name_plural = "Local Video File Caches"
        ordering = ['-last_accessed_at']
        indexes = [
            models.Index(fields=['folder_name', 'duration']),
            models.Index(fields=['file_path', 'modified_time']),
            models.Index(fields=['-last_accessed_at']),
        ]
    
    def __str__(self) -> str:
        return f"{self.file_path} ({self.duration:.1f}s)"
    
    def is_valid(self) -> bool:
        """Check if cache is still valid (file exists and not modified)."""
        import os
        from datetime import datetime
        
        if not os.path.exists(self.file_path):
            return False
        
        try:
            stat = os.stat(self.file_path)
            # Compare file size and mtime
            if stat.st_size != self.file_size:
                return False
            
            file_mtime = datetime.fromtimestamp(stat.st_mtime)
            # Allow 1 second tolerance for filesystem timestamp precision
            time_diff = abs((file_mtime - self.modified_time).total_seconds())
            return time_diff < 1.0
        except (OSError, IOError):
            return False



# Lark Suite Integration Models (Managed by Prisma)
class LarkReport(models.Model):
    id = models.CharField(max_length=255, primary_key=True)
    name = models.CharField(max_length=255)
    team = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    answers = models.JSONField(null=True, blank=True)
    date = models.DateTimeField(null=True, blank=True)
    email = models.CharField(max_length=255, null=True, blank=True)
    employee = models.JSONField(null=True, blank=True)
    role = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = 'lark_reports'
        managed = False

class AppUser(models.Model):
    """Đồng bộ với bảng users (Prisma): nhân viên Lark nằm trực tiếp trên user."""
    id = models.CharField(max_length=255, primary_key=True)
    email = models.CharField(max_length=255, unique=True)
    full_name = models.CharField(max_length=255)
    roles = models.TextField(db_column='roles', null=True, blank=True)
    manager_id = models.CharField(max_length=255, null=True, blank=True)
    team = models.CharField(max_length=255, null=True, blank=True)
    image_url = models.TextField(null=True, blank=True)
    employee_data = models.JSONField(null=True, blank=True)
    lark_employee_record_id = models.CharField(max_length=255, null=True, blank=True)
    employee_id = models.CharField(max_length=255, null=True, blank=True)
    employee_position = models.CharField(max_length=255, null=True, blank=True)
    employee_status = models.CharField(max_length=255, null=True, blank=True)
    employee_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'users'
        managed = False

class LarkPermission(models.Model):
    id = models.CharField(max_length=255, primary_key=True)
    email = models.CharField(max_length=255, null=True, blank=True)
    name = models.CharField(max_length=255, null=True, blank=True)
    pin_code = models.CharField(max_length=255, null=True, blank=True)
    employee = models.JSONField(null=True, blank=True)
    role = models.CharField(max_length=255, null=True, blank=True)
    team = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=255, null=True, blank=True)
    permissions = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'lark_permissions'
        managed = False

class ReportOutstanding(models.Model):
    id = models.CharField(max_length=255, primary_key=True)
    name = models.CharField(max_length=255)
    date = models.CharField(max_length=100, null=True, blank=True)
    team = models.CharField(max_length=255, null=True, blank=True)
    role = models.CharField(max_length=255, null=True, blank=True)

    category = models.TextField(db_column='category', null=True, blank=True)
    content = models.TextField(db_column='content', null=True, blank=True)
    status = models.CharField(max_length=255, null=True, blank=True)

    email = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'report_outstanding'
        managed = False

class ReportSettings(BaseModel):
    """
    Settings for reporting checklist, including allowed time ranges and randomization.
    """
    schedule = models.JSONField(
        default=dict,
        help_text="Khung giờ báo cáo theo ngày (monday, tuesday, ...)"
    )
    one_report_per_day = models.BooleanField(
        default=True,
        help_text="Chỉ cho phép submit 1 báo cáo/ngày"
    )
    timezone = models.CharField(
        max_length=50,
        default='Asia/Ho_Chi_Minh'
    )
    # Fields for random reporting
    is_random = models.BooleanField(
        default=False,
        help_text="Bật chế độ báo cáo ngẫu nhiên"
    )
    random_minutes = models.IntegerField(
        default=30,
        help_text="Số phút báo cáo ngẫu nhiên trong khung giờ"
    )
    updated_by = models.CharField(
        max_length=255,
        blank=True
    )

    class Meta:
        verbose_name = "Report Settings"
        verbose_name_plural = "Report Settings"

    def __str__(self):
        return f"Report Settings (Updated by: {self.updated_by})"


# Import search-related models
from .models_search import SearchQuery, TrendingKeyword

# Import scraper models (Facebook Fanpage & Reels discovery system)
from .models_scraper import (
    SearchKeyword, ScrapedFanpage, FanpageMetricsHistory,
    FacebookReel, ReelMetricsHistory,
    TikTokVideo, TikTokProfile, TikTokProfileVideo,
    InstagramProfile, InstagramReel, InstagramProfileMetrics,
)

# End of models

