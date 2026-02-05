"""
Serializers for API request/response data validation.

This module defines serializers for all API endpoints, handling
validation, serialization, and deserialization of data.
"""

from rest_framework import serializers
from .models import (
    SearchHistory, ScrapedVideo, TrackedChannel, Platform,
    VideoCollection, CollectionVideo
)


class SearchRequestSerializer(serializers.Serializer):
    """Serializer for search request validation."""
    
    platform = serializers.ChoiceField(
        choices=[p.value for p in Platform],
        help_text="Platform to search (tiktok, instagram, facebook, douyin)"
    )
    keyword = serializers.CharField(
        max_length=500,
        help_text="Search keyword or hashtag"
    )
    min_likes = serializers.IntegerField(
        default=0,
        min_value=0,
        required=False,
        help_text="Minimum likes filter"
    )
    min_views = serializers.IntegerField(
        default=0,
        min_value=0,
        required=False,
        help_text="Minimum views filter"
    )
    max_results = serializers.IntegerField(
        default=20,
        min_value=1,
        max_value=10000,
        required=False,
        help_text="Maximum number of results (1-10000)"
    )
    use_cache = serializers.BooleanField(
        default=True,
        required=False,
        help_text="Whether to use cached results if available"
    )
    async_mode = serializers.BooleanField(
        default=False,
        required=False,
        help_text="Run search asynchronously using Celery"
    )
    search_type = serializers.CharField(
        required=False,
        default='posts',
        help_text="Specific content type (e.g., 'reels', 'posts')"
    )


class UserVideosRequestSerializer(serializers.Serializer):
    """Serializer for user videos request."""
    
    platform = serializers.ChoiceField(
        choices=[p.value for p in Platform],
        help_text="Platform to search"
    )
    username = serializers.CharField(
        max_length=255,
        help_text="Username or user ID"
    )
    max_results = serializers.IntegerField(
        default=9999,
        min_value=1,
        required=False,
        help_text="Maximum number of results (default: 9999 for all)"
    )
    until_date = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Fetch videos from this start date (YYYY-MM-DD)"
    )
    start_date = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Start date for filtering (YYYY-MM-DD)"
    )
    end_date = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="End date for filtering (YYYY-MM-DD)"
    )



class VideoSerializer(serializers.ModelSerializer):
    """Serializer for scraped video data."""
    
    engagement_rate = serializers.SerializerMethodField()
    is_video = serializers.SerializerMethodField()
    platform_display = serializers.CharField(source='get_platform_display', read_only=True)
    
    class Meta:
        model = ScrapedVideo
        fields = [
            'id',
            'platform',
            'platform_display',
            'video_id',
            'title',
            'description',
            'author_username',
            'author_name',
            'likes_count',
            'views_count',
            'comments_count',
            'shares_count',
            'engagement_rate',
            'is_video',
            'video_url',
            'download_url',
            'thumbnail_url',
            'published_at',
            'hashtags',
            'music_info',
            'raw_data',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_engagement_rate(self, obj):
        """Calculate engagement rate percentage."""
        return round(obj.engagement_rate, 2)
        
    def get_is_video(self, obj):
        """Determine if this is a video content."""
        # 1. Check raw_data (injected from scraper)
        if obj.raw_data and isinstance(obj.raw_data, dict):
            if 'is_video_derived' in obj.raw_data:
                return bool(obj.raw_data['is_video_derived'])
            # Fallback for old data or other scrapers
            if obj.raw_data.get('isVideo'):
                return True
                
        # 2. Check URL presence
        if obj.video_url or obj.download_url:
            return True
            
        # 3. Platform specific
        if obj.platform in [Platform.TIKTOK, Platform.DOUYIN]:
            return True
            
        return False


class SearchHistorySerializer(serializers.ModelSerializer):
    """Serializer for search history."""
    
    platform_display = serializers.CharField(source='get_platform_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    is_expired = serializers.SerializerMethodField()
    videos_count = serializers.SerializerMethodField()
    
    class Meta:
        model = SearchHistory
        fields = [
            'id',
            'platform',
            'platform_display',
            'keyword',
            'status',
            'status_display',
            'min_likes',
            'min_views',
            'max_results',
            'results_count',
            'task_id',
            'error_message',
            'execution_time',
            'is_expired',
            'expires_at',
            'videos_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_is_expired(self, obj):
        """Check if cache is expired."""
        return obj.is_expired()
    
    def get_videos_count(self, obj):
        """Get count of associated videos."""
        return obj.videos.count()


class SearchResultSerializer(serializers.Serializer):
    """Serializer for search result response."""
    
    success = serializers.BooleanField()
    cached = serializers.BooleanField(default=False)
    async_mode = serializers.BooleanField(default=False)
    task_id = serializers.CharField(required=False, allow_null=True)
    search_id = serializers.IntegerField(required=False, allow_null=True)
    count = serializers.IntegerField()
    execution_time = serializers.FloatField()
    results = VideoSerializer(many=True, required=False)
    error = serializers.CharField(required=False, allow_null=True)
    message = serializers.CharField(required=False)


class TaskStatusSerializer(serializers.Serializer):
    """Serializer for async task status."""
    
    task_id = serializers.CharField()
    status = serializers.CharField()
    ready = serializers.BooleanField()
    successful = serializers.BooleanField(required=False, allow_null=True)
    result = serializers.JSONField(required=False, allow_null=True)
    error = serializers.CharField(required=False, allow_null=True)
    traceback = serializers.CharField(required=False, allow_null=True)


class TrackedChannelSerializer(serializers.ModelSerializer):
    """Serializer for tracked channels."""
    
    platform_display = serializers.CharField(source='get_platform_display', read_only=True)
    videos_count = serializers.SerializerMethodField()
    total_likes = serializers.SerializerMethodField()
    total_views = serializers.SerializerMethodField()
    engagement = serializers.SerializerMethodField()
    engagement_rate = serializers.SerializerMethodField()
    should_check = serializers.SerializerMethodField()
    
    class Meta:
        model = TrackedChannel
        fields = [
            'id',
            'platform',
            'platform_display',
            'channel_id',
            'username',
            'display_name',
            'is_active',
            'check_interval_minutes',
            'min_likes_threshold',
            'last_checked_at',
            'follower_count',
            'videos_count',
            'total_likes',
            'total_views',
            'engagement',
            'engagement_rate',
            'should_check',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'last_checked_at', 'created_at', 'updated_at']
    
    def get_videos_count(self, obj):
        """Get count of videos from this channel."""
        return ScrapedVideo.objects.filter(
            platform=obj.platform,
            author_username=obj.username
        ).count()
    
    def get_total_likes(self, obj):
        """Get total likes from all videos of this channel."""
        from django.db.models import Sum
        result = ScrapedVideo.objects.filter(
            platform=obj.platform,
            author_username=obj.username
        ).aggregate(total=Sum('likes_count'))
        return result['total'] or 0
    
    def get_total_views(self, obj):
        """Get total views from all videos of this channel."""
        from django.db.models import Sum
        result = ScrapedVideo.objects.filter(
            platform=obj.platform,
            author_username=obj.username
        ).aggregate(total=Sum('views_count'))
        return result['total'] or 0
    
    def get_engagement(self, obj):
        """Get total engagement (likes + comments) from all videos of this channel."""
        from django.db.models import Sum
        result = ScrapedVideo.objects.filter(
            platform=obj.platform,
            author_username=obj.username
        ).aggregate(
            total_likes=Sum('likes_count'),
            total_comments=Sum('comments_count')
        )
        total_likes = result['total_likes'] or 0
        total_comments = result['total_comments'] or 0
        return total_likes + total_comments
    
    def get_engagement_rate(self, obj):
        """Calculate average engagement rate across all videos of this channel."""
        from django.db.models import Sum
        result = ScrapedVideo.objects.filter(
            platform=obj.platform,
            author_username=obj.username
        ).aggregate(
            total_likes=Sum('likes_count'),
            total_comments=Sum('comments_count'),
            total_views=Sum('views_count')
        )
        total_likes = result['total_likes'] or 0
        total_comments = result['total_comments'] or 0
        total_views = result['total_views'] or 0
        
        if total_views == 0:
            return 0.0
        
        engagement_rate = ((total_likes + total_comments) / total_views) * 100
        return round(engagement_rate, 2)
    
    def get_should_check(self, obj):
        """Check if channel should be checked now."""
        if not obj.is_active:
            return False
        
        if not obj.last_checked_at:
            return True
        
        from django.utils import timezone
        from datetime import timedelta
        
        next_check = obj.last_checked_at + timedelta(minutes=obj.check_interval_minutes)
        return timezone.now() >= next_check


class StatsSerializer(serializers.Serializer):
    """Serializer for general statistics."""
    
    total_videos = serializers.IntegerField()
    total_searches = serializers.IntegerField()
    total_channels = serializers.IntegerField()
    videos_by_platform = serializers.DictField()
    searches_by_platform = serializers.DictField()
    top_videos = VideoSerializer(many=True)
    recent_searches = SearchHistorySerializer(many=True)


class CollectionVideoSerializer(serializers.ModelSerializer):
    """Serializer for videos in a collection."""
    
    video = VideoSerializer(read_only=True)
    video_id = serializers.IntegerField(write_only=True)
    
    class Meta:
        model = CollectionVideo
        fields = [
            'id',
            'video',
            'video_id',
            'notes',
            'order',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class VideoCollectionSerializer(serializers.ModelSerializer):
    """Serializer for video collections."""
    
    video_count = serializers.SerializerMethodField()
    collection_videos = CollectionVideoSerializer(many=True, read_only=True)
    
    class Meta:
        model = VideoCollection
        fields = [
            'id',
            'name',
            'description',
            'color',
            'video_count',
            'collection_videos',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_video_count(self, obj):
        """Get number of videos in collection."""
        return obj.video_count


class VideoCollectionListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for collection list (without videos)."""
    
    video_count = serializers.SerializerMethodField()
    
    class Meta:
        model = VideoCollection
        fields = [
            'id',
            'name',
            'description',
            'color',
            'video_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_video_count(self, obj):
        """Get number of videos in collection."""
        return obj.video_count


class AddVideoToCollectionSerializer(serializers.Serializer):
    """Serializer for adding a video to collection."""
    
    video_id = serializers.IntegerField(help_text="ID of the video to add")
    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional notes about this video"
    )
    order = serializers.IntegerField(
        required=False,
        default=0,
        help_text="Display order"
    )


class AddVideosToCollectionSerializer(serializers.Serializer):
    """Serializer for adding multiple videos to collection."""
    
    video_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="List of video IDs to add"
    )
    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional notes"
    )

