"""
Django admin configuration for video management models.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.utils.safestring import mark_safe
from .models import SearchHistory, ScrapedVideo, TrackedChannel, VideoCollection, CollectionVideo, FacebookPageCache


@admin.register(SearchHistory)
class SearchHistoryAdmin(admin.ModelAdmin):
    """Admin interface for SearchHistory model."""
    
    list_display = [
        'id',
        'platform',
        'keyword',
        'status_badge',
        'results_count',
        'execution_time',
        'cached_badge',
        'created_at',
    ]
    list_filter = ['platform', 'status', 'created_at']
    search_fields = ['keyword', 'task_id']
    readonly_fields = [
        'id',
        'task_id',
        'results_count',
        'execution_time',
        'created_at',
        'updated_at',
        'expires_at',
    ]
    fieldsets = (
        ('Search Info', {
            'fields': ('platform', 'keyword', 'status')
        }),
        ('Filters', {
            'fields': ('min_likes', 'min_views', 'max_results')
        }),
        ('Results', {
            'fields': ('results_count', 'execution_time', 'raw_results')
        }),
        ('Task Info', {
            'fields': ('task_id', 'error_message')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at', 'expires_at'),
            'classes': ('collapse',)
        }),
    )
    date_hierarchy = 'created_at'
    
    def status_badge(self, obj):
        """Display colored status badge."""
        colors = {
            'pending': 'gray',
            'processing': 'blue',
            'completed': 'green',
            'failed': 'red',
            'cached': 'purple',
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display()
        )
    status_badge.short_description = 'Status'
    
    def cached_badge(self, obj):
        """Display cache status."""
        if obj.is_expired():
            return format_html('<span style="color: red;">❌ Expired</span>')
        return format_html('<span style="color: green;">✅ Valid</span>')
    cached_badge.short_description = 'Cache'


@admin.register(ScrapedVideo)
class ScrapedVideoAdmin(admin.ModelAdmin):
    """Admin interface for ScrapedVideo model."""
    
    list_display = [
        'id',
        'platform',
        'video_preview',
        'author_username',
        'engagement_metrics',
        'published_at',
        'created_at',
    ]
    list_filter = ['platform', 'created_at', 'published_at']
    search_fields = ['video_id', 'title', 'author_username', 'author_name']
    readonly_fields = [
        'id',
        'video_id',
        'engagement_rate',
        'created_at',
        'updated_at',
        'video_link',
        'thumbnail_preview',
    ]
    fieldsets = (
        ('Platform Info', {
            'fields': ('platform', 'video_id', 'video_link', 'thumbnail_preview')
        }),
        ('Content', {
            'fields': ('title', 'description')
        }),
        ('Author', {
            'fields': ('author_username', 'author_name')
        }),
        ('Engagement', {
            'fields': (
                'likes_count',
                'views_count',
                'comments_count',
                'shares_count',
                'engagement_rate',
            )
        }),
        ('URLs', {
            'fields': ('video_url', 'download_url', 'thumbnail_url'),
            'classes': ('collapse',)
        }),
        ('Additional Data', {
            'fields': ('hashtags', 'music_info', 'raw_data'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('published_at', 'search_history', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    date_hierarchy = 'created_at'
    
    def video_preview(self, obj):
        """Display video title preview."""
        title = obj.title[:50] + '...' if len(obj.title) > 50 else obj.title
        return format_html(
            '<strong>{}</strong><br><small style="color: gray;">{}</small>',
            title,
            obj.video_id
        )
    video_preview.short_description = 'Video'
    
    def engagement_metrics(self, obj):
        """Display engagement metrics."""
        return format_html(
            '❤️ {} | 👁️ {} | 💬 {} | 📊 {:.1f}%',
            obj.likes_count,
            obj.views_count,
            obj.comments_count,
            obj.engagement_rate
        )
    engagement_metrics.short_description = 'Engagement'
    
    def video_link(self, obj):
        """Display clickable video link."""
        if obj.video_url:
            return format_html(
                '<a href="{}" target="_blank">Open Video</a>',
                obj.video_url
            )
        return '-'
    video_link.short_description = 'Video Link'
    
    def thumbnail_preview(self, obj):
        """Display thumbnail image."""
        if obj.thumbnail_url:
            return format_html(
                '<img src="{}" style="max-width: 200px; max-height: 200px;" />',
                obj.thumbnail_url
            )
        return '-'
    thumbnail_preview.short_description = 'Thumbnail'


@admin.register(TrackedChannel)
class TrackedChannelAdmin(admin.ModelAdmin):
    """Admin interface for TrackedChannel model."""
    
    list_display = [
        'id',
        'platform',
        'username',
        'display_name',
        'is_active',
        'videos_count',
        'last_checked_at',
        'check_actions',
    ]
    list_filter = ['platform', 'is_active', 'created_at']
    search_fields = ['username', 'display_name', 'channel_id']
    readonly_fields = [
        'id',
        'last_checked_at',
        'created_at',
        'updated_at',
        'videos_count_display',
    ]
    fieldsets = (
        ('Channel Info', {
            'fields': ('platform', 'channel_id', 'username', 'display_name')
        }),
        ('Tracking Settings', {
            'fields': (
                'is_active',
                'check_interval_minutes',
                'min_likes_threshold',
            )
        }),
        ('Metadata', {
            'fields': ('follower_count', 'last_checked_at', 'videos_count_display')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    actions = ['activate_channels', 'deactivate_channels']
    date_hierarchy = 'created_at'
    
    def videos_count(self, obj):
        """Count videos from this channel."""
        count = ScrapedVideo.objects.filter(
            platform=obj.platform,
            author_username=obj.username
        ).count()
        return count
    videos_count.short_description = 'Videos'
    
    def videos_count_display(self, obj):
        """Display video count with link."""
        count = self.videos_count(obj)
        url = f"{reverse('admin:video_management_scrapedvideo_changelist')}?author_username={obj.username}"
        return format_html(
            '<a href="{}">{} videos</a>',
            url,
            count
        )
    videos_count_display.short_description = 'Videos Count'
    
    def check_actions(self, obj):
        """Display action buttons."""
        if obj.is_active:
            status = format_html('<span style="color: green;">✅ Active</span>')
        else:
            status = format_html('<span style="color: gray;">⏸️ Inactive</span>')
        return status
    check_actions.short_description = 'Status'
    
    def activate_channels(self, request, queryset):
        """Activate selected channels."""
        count = queryset.update(is_active=True)
        self.message_user(request, f'{count} channel(s) activated.')
    activate_channels.short_description = 'Activate selected channels'
    
    def deactivate_channels(self, request, queryset):
        """Deactivate selected channels."""
        count = queryset.update(is_active=False)
        self.message_user(request, f'{count} channel(s) deactivated.')
    deactivate_channels.short_description = 'Deactivate selected channels'
    
    # DISABLED: Background task removed - use manual API endpoint instead
    # def check_now(self, request, queryset):
    #     """Trigger check for selected channels."""
    #     from .tasks import check_channel_task
    #     
    #     count = 0
    #     for channel in queryset:
    #         check_channel_task.delay(channel.id)
    #         count += 1
    #     
    #     self.message_user(request, f'Started check for {count} channel(s).')
    # check_now.short_description = 'Check selected channels now'


class CollectionVideoInline(admin.TabularInline):
    """Inline admin for videos in a collection."""
    model = CollectionVideo
    extra = 0
    fields = ['video', 'notes', 'order', 'created_at']
    readonly_fields = ['created_at']
    autocomplete_fields = ['video']


@admin.register(VideoCollection)
class VideoCollectionAdmin(admin.ModelAdmin):
    """Admin interface for VideoCollection model."""
    
    list_display = [
        'id',
        'name',
        'color_badge',
        'video_count_display',
        'created_at',
    ]
    search_fields = ['name', 'description']
    readonly_fields = ['id', 'created_at', 'updated_at', 'video_count_display']
    inlines = [CollectionVideoInline]
    fieldsets = (
        ('Collection Info', {
            'fields': ('name', 'description', 'color')
        }),
        ('Statistics', {
            'fields': ('video_count_display',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    date_hierarchy = 'created_at'
    
    def color_badge(self, obj):
        """Display color badge."""
        return format_html(
            '<span style="background-color: {}; padding: 5px 15px; border-radius: 3px; color: white;">{}</span>',
            obj.color,
            obj.color
        )
    color_badge.short_description = 'Color'
    
    def video_count_display(self, obj):
        """Display video count."""
        count = obj.video_count
        return format_html(
            '<strong>{}</strong> videos',
            count
        )
    video_count_display.short_description = 'Videos'


@admin.register(CollectionVideo)
class CollectionVideoAdmin(admin.ModelAdmin):
    """Admin interface for CollectionVideo model."""
    
    list_display = [
        'id',
        'collection',
        'video_preview',
        'order',
        'created_at',
    ]
    list_filter = ['collection', 'created_at']
    search_fields = ['collection__name', 'video__title', 'notes']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['collection', 'video']
    fieldsets = (
        ('Relationship', {
            'fields': ('collection', 'video')
        }),
        ('Details', {
            'fields': ('notes', 'order')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    date_hierarchy = 'created_at'
    
    def video_preview(self, obj):
        """Display video preview."""
        return format_html(
            '<strong>{}</strong><br><small>@{}</small>',
            obj.video.title[:50],
            obj.video.author_username
        )
    video_preview.short_description = 'Video'


@admin.register(FacebookPageCache)
class FacebookPageCacheAdmin(admin.ModelAdmin):
    """Admin interface for FacebookPageCache model."""
    
    list_display = [
        'username',
        'page_name',
        'followers_count',
        'likes_count',
        'cache_status',
        'last_fetched_at',
        'expires_at',
    ]
    search_fields = ['username', 'page_name']
    readonly_fields = [
        'last_fetched_at',
        'created_at',
        'updated_at',
        'cache_age_display',
    ]
    fieldsets = (
        ('Page Info', {
            'fields': ('username', 'page_name', 'avatar_url')
        }),
        ('Statistics', {
            'fields': ('followers_count', 'likes_count', 'verified')
        }),
        ('Additional Info', {
            'fields': ('page_description', 'page_category'),
            'classes': ('collapse',)
        }),
        ('Cache Control', {
            'fields': ('expires_at', 'last_fetched_at', 'cache_age_display')
        }),
        ('Raw Data', {
            'fields': ('raw_data',),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    date_hierarchy = 'last_fetched_at'
    actions = ['refresh_cache', 'clear_expired']
    
    def cache_status(self, obj):
        """Display cache status with badge."""
        if obj.is_expired():
            return format_html(
                '<span style="color: red; font-weight: bold;">❌ Expired</span>'
            )
        from django.utils import timezone
        remaining = obj.expires_at - timezone.now()
        hours = int(remaining.total_seconds() / 3600)
        return format_html(
            '<span style="color: green; font-weight: bold;">✅ Valid ({} hrs left)</span>',
            hours
        )
    cache_status.short_description = 'Cache Status'
    
    def cache_age_display(self, obj):
        """Display how long ago data was fetched."""
        from django.utils import timezone
        age = timezone.now() - obj.last_fetched_at
        hours = int(age.total_seconds() / 3600)
        return f"{hours} hours ago"
    cache_age_display.short_description = 'Cache Age'
    
    def refresh_cache(self, request, queryset):
        """Refresh cache for selected entries."""
        count = 0
        for obj in queryset:
            obj.refresh_expiry()
            count += 1
        self.message_user(request, f'{count} cache entry(ies) refreshed.')
    refresh_cache.short_description = 'Refresh cache expiry'
    
    def clear_expired(self, request, queryset):
        """Clear expired cache entries."""
        count = 0
        for obj in queryset:
            if obj.is_expired():
                obj.delete()
                count += 1
        self.message_user(request, f'{count} expired cache entry(ies) cleared.')
    clear_expired.short_description = 'Clear expired entries'



# Customize admin site
admin.site.site_header = 'AutomationGenVideo AI Admin'
admin.site.site_title = 'Video Management'
admin.site.index_title = 'Video Scraping Management'
