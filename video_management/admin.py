from django.contrib import admin
from .models import TrackedChannel, ReportedVideo, SearchCache

@admin.register(TrackedChannel)
class TrackedChannelAdmin(admin.ModelAdmin):
    list_display = ('name', 'channel_id', 'threshold_likes', 'is_active', 'created_at', 'updated_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('name', 'channel_id')
    readonly_fields = ('created_at', 'updated_at')

@admin.register(ReportedVideo)
class ReportedVideoAdmin(admin.ModelAdmin):
    list_display = ('video_id', 'channel', 'likes_at_report', 'timestamp')
    list_filter = ('timestamp', 'channel')
    search_fields = ('video_id', 'channel__name')
    readonly_fields = ('timestamp',)

@admin.register(SearchCache)
class SearchCacheAdmin(admin.ModelAdmin):
    list_display = ('keyword', 'created_at', 'expires_at')
    list_filter = ('created_at', 'expires_at')
    search_fields = ('keyword',)
    readonly_fields = ('created_at',)
