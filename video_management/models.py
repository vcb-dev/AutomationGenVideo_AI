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


class Platform(models.TextChoices):
    """Supported social media platforms."""
    TIKTOK = 'tiktok', 'TikTok'
    DOUYIN = 'douyin', 'Douyin'
    INSTAGRAM = 'instagram', 'Instagram'
    FACEBOOK = 'facebook', 'Facebook'


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
            
            # Store in cache
            cache = cls.objects.create(
                username=username,
                page_name=page_info.get('name', username),
                avatar_url=page_info.get('avatar_url', ''),
                followers_count=page_info.get('followers', 0),
                likes_count=page_info.get('likes', 0),
                page_description=page_info.get('description', ''),
                page_category=page_info.get('category', ''),
                verified=page_info.get('verified', False),
                raw_data=page_info,
                expires_at=timezone.now() + timedelta(hours=24)
            )
            
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
