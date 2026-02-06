"""
URL routing for video management API.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    # Search views
    SearchView,
    SearchStatusView,
    SearchHistoryView,
    UserVideosView,
    VideosByChannelView,
    # Channel views
    ChannelListCreateView,
    ChannelDetailView,
    ChannelCheckView,
    ChannelCheckByUsernameView,
    # Stats views
    StatsView,
    HealthCheckView,
    # Mix views
    mix_videos,
    mix_status,
    mix_cancel,
)
from .views.collection_views import VideoCollectionViewSet
from .views.channel_hashtag_stats_views import get_channel_hashtag_stats
from .views.facebook_analysis_views import (
    analyze_facebook_url,
    get_available_methods,
    detect_facebook_type,
)
from .views.douyin_search_views import search_douyin_videos
from .views.xiaohongshu_search_views import search_xiaohongshu_notes
from .views.tiktok_search_views import search_tiktok_videos

app_name = 'video_management'

# Router for ViewSets
router = DefaultRouter()
router.register(r'collections', VideoCollectionViewSet, basename='collection')

urlpatterns = [
    # Health check
    path('health/', HealthCheckView.as_view(), name='health'),
    
    # Search endpoints
    path('search/', SearchView.as_view(), name='search'),
    path('search/status/<str:task_id>/', SearchStatusView.as_view(), name='search-status'),
    path('search/history/', SearchHistoryView.as_view(), name='search-history'),
    path('search/user-videos/', UserVideosView.as_view(), name='user-videos'),
    path('videos/by-channel/', VideosByChannelView.as_view(), name='videos-by-channel'),
    
    path('videos/channel-hashtag-stats/', get_channel_hashtag_stats, name='channel-hashtag-stats'),
    path('videos/mix/', mix_videos, name='mix-videos'),
    path('videos/mix/cancel/<str:progress_id>/', mix_cancel, name='mix-cancel'),
    path('videos/mix/status/<str:progress_id>/', mix_status, name='mix-status'),
    
    # Channel endpoints
    path('channels/', ChannelListCreateView.as_view(), name='channel-list'),
    path('channels/check-by-username/', ChannelCheckByUsernameView.as_view(), name='channel-check-by-username'),
    path('channels/<int:pk>/', ChannelDetailView.as_view(), name='channel-detail'),
    path('channels/<int:pk>/check/', ChannelCheckView.as_view(), name='channel-check'),
    
    # Statistics
    path('stats/', StatsView.as_view(), name='stats'),
    
    # Facebook Analysis
    path('facebook/analyze/', analyze_facebook_url, name='facebook-analyze'),
    path('facebook/methods/', get_available_methods, name='facebook-methods'),
    path('facebook/detect/', detect_facebook_type, name='facebook-detect'),
    
    # Douyin Search
    path('douyin/search/', search_douyin_videos, name='douyin-search'),
    
    # Xiaohongshu Search
    path('xiaohongshu/search/', search_xiaohongshu_notes, name='xiaohongshu-search'),
    
    # TikTok Search (TikHub)
    path('tiktok/search-v2/', search_tiktok_videos, name='tiktok-search-v2'),
    
    # Collections (router URLs)
    path('', include(router.urls)),
]
