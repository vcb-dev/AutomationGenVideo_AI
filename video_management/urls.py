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
    # Mix views (OLD - DEPRECATED)
    mix_videos,
    mix_videos_auto,
    mix_videos_upload,
    mix_status,
    mix_cancel,
    scan_folder,
    scan_folder_batch,
    video_cache_manage,
    # Smart Mix views (NEW)
    smart_mix,
    smart_mix_status,
    index_folders,
    index_outro,
    cache_stats,
    get_voices,
    generate_audio_from_script,
    serve_cached_audio,
    index_manufacturing_folder,
    # Pre-generation views
    pregen_status,
    pregen_start,
    pregen_cancel,
)
from .views.collection_views import VideoCollectionViewSet
from .views.channel_hashtag_stats_views import get_channel_hashtag_stats
from .views.facebook_analysis_views import (
    analyze_facebook_url,
    get_available_methods,
    detect_facebook_type,
)
from .views.douyin_search_views import search_douyin_videos
from .views.douyin_profile_views import fetch_douyin_channel_profile
from .views.xiaohongshu_search_views import search_xiaohongshu_notes
from .views.tiktok_search_views import search_tiktok_videos
from .views.tiktok_suggest_views import tiktok_search_suggest
from .views.product_views import (
    upload_product_catalog,
    list_product_catalogs,
    get_product_catalog,
    get_products_by_category,
    delete_product_catalog,
    get_product_detail,
    find_product_video_path,
)
from .views.suggestions_views import (
    get_search_suggestions,
    track_search,
)
from .views.image_proxy_views import ImageProxyView
from .views.virtual_mix_views import (
    virtual_mix,
    stream_video,
    stream_clip,
    stream_audio,
    virtual_mix_render,
)
from .views.checklist_views import ChecklistSubmitView, ChecklistCheckView
from .views.translation_views import translate_to_chinese

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
    
    # Search Suggestions (NEW)
    path('search/suggestions/', get_search_suggestions, name='search-suggestions'),
    path('search/track/', track_search, name='track-search'),
    path('search/translate/', translate_to_chinese, name='search-translate'),

    # Media fetch (bypass CORS; avoid "image-proxy" name which triggers ad blockers)
    path('image-proxy/', ImageProxyView.as_view(), name='image-proxy'),  # legacy
    path('media/', ImageProxyView.as_view(), name='media'),
    
    path('videos/channel-hashtag-stats/', get_channel_hashtag_stats, name='channel-hashtag-stats'),
    
    # OLD Mix endpoints (DEPRECATED - slow 2-3 min)
    path('videos/mix/', mix_videos, name='mix-videos'),
    path('videos/mix-auto/', mix_videos_auto, name='mix-videos-auto'),
    path('videos/mix-upload/', mix_videos_upload, name='mix-videos-upload'),
    path('videos/mix/cancel/<str:progress_id>/', mix_cancel, name='mix-cancel'),
    path('videos/mix/status/<str:progress_id>/', mix_status, name='mix-status'),
    path('videos/scan-folder/', scan_folder, name='scan-folder'),
    path('videos/scan-folder-batch/', scan_folder_batch, name='scan-folder-batch'),
    path('videos/cache/', video_cache_manage, name='video-cache-manage'),
    
    # NEW Smart Mix endpoints (20-30x faster!)
    path('videos/smart-mix/', smart_mix, name='smart-mix'),
    path('videos/smart-mix/status/<str:progress_id>/', smart_mix_status, name='smart-mix-status'),
    path('videos/index-folders/', index_folders, name='index-folders'),
    path('videos/index-manufacturing-folder/', index_manufacturing_folder, name='index-manufacturing-folder'),
    path('videos/index-outro/', index_outro, name='index-outro'),
    path('videos/cache-stats/', cache_stats, name='cache-stats'),
    path('videos/voices/', get_voices, name='get-voices'),
    path('videos/generate-audio/', generate_audio_from_script, name='generate-audio'),
    
    # Pre-generation endpoints (background clip generation)
    path('videos/pregen/status/', pregen_status, name='pregen-status'),
    path('videos/pregen/start/', pregen_start, name='pregen-start'),
    path('videos/pregen/cancel/', pregen_cancel, name='pregen-cancel'),
    
    # Virtual Mix endpoints (INSTANT preview, no FFmpeg!)
    path('videos/virtual-mix/', virtual_mix, name='virtual-mix'),
    path('videos/virtual-mix/render/', virtual_mix_render, name='virtual-mix-render'),
    path('videos/stream/<int:video_id>/', stream_video, name='stream-video'),
    path('videos/stream-clip/<int:clip_id>/', stream_clip, name='stream-clip'),
    path('videos/stream-audio/<str:audio_id>/', stream_audio, name='stream-audio'),
    
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
    
    # Douyin Channel Profile (full: followers, avatar, engagement) — called on Update only
    path('douyin/profile/', fetch_douyin_channel_profile, name='douyin-channel-profile'),
    
    # Xiaohongshu Search
    path('xiaohongshu/search/', search_xiaohongshu_notes, name='xiaohongshu-search'),
    
    # TikTok Search (TikHub)
    path('tiktok/search-v2/', search_tiktok_videos, name='tiktok-search-v2'),
    
    # TikTok Search Suggest (Real-time autocomplete)
    path('tiktok/suggest/', tiktok_search_suggest, name='tiktok-suggest'),
    
    # Product Catalog Management
    path('products/upload/', upload_product_catalog, name='product-upload'),
    path('products/catalogs/', list_product_catalogs, name='product-catalogs'),
    path('products/catalogs/<int:catalog_id>/', get_product_catalog, name='product-catalog-detail'),
    path('products/catalogs/<int:catalog_id>/by-category/', get_products_by_category, name='products-by-category'),
    path('products/catalogs/<int:catalog_id>/delete/', delete_product_catalog, name='product-catalog-delete'),
    path('products/<int:product_id>/', get_product_detail, name='product-detail'),
    path('products/find-video/', find_product_video_path, name='find-product-video'),
    
    # Checklist công việc -> Lark Bitable
    path('checklist/check/', ChecklistCheckView.as_view(), name='checklist-check'),
    path('checklist/submit/', ChecklistSubmitView.as_view(), name='checklist-submit'),
    
    # Collections (router URLs)
    path('', include(router.urls)),
]
