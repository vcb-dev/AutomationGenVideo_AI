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
    clear_index,
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
from .views.facebook_views import facebook_sync, facebook_import, facebook_backfill, get_managed_pages, get_synced_videos
from .views.scraper_views import (
    all_external_videos,
    keyword_suggest, keyword_hit, keyword_list, keyword_create,
    trigger_discovery, discovered_fanpages, fanpage_detail, fanpage_toggle,
    search_reels, trigger_scrape_reels, fanpage_scrape_by_url,
    tiktok_search, tiktok_videos, tiktok_keyword_suggest,
    tiktok_profile_scrape, tiktok_profiles_list, tiktok_profile_detail,
    tiktok_profile_videos, tiktok_profile_toggle,
    instagram_profile_scrape, instagram_profiles_list, instagram_profile_detail,
    instagram_profile_reels, instagram_profile_toggle,
    douyin_keyword_search, douyin_videos_list, douyin_keyword_suggest,
    douyin_profile_scrape, douyin_profiles_list,
    douyin_profile_detail, douyin_profile_toggle, douyin_profile_videos,
    owned_channel_videos,
)
from .views.channel_hashtag_stats_views import get_channel_hashtag_stats
from .views.facebook_analysis_views import (
    analyze_facebook_url,
    get_available_methods,
    detect_facebook_type,
    analyze_facebook_competitor,
    facebook_channel_metrics,
)
from .views.channel_analysis_generic_views import (
    channel_insights_generic,
    channel_metrics_generic,
    channel_analysis_unified_generic,
)
from .views.douyin_search_views import search_douyin_videos
from .views.douyin_profile_views import fetch_douyin_channel_profile
from .views.xiaohongshu_search_views import (
    search_xiaohongshu_notes, list_xiaohongshu_videos, xiaohongshu_keyword_suggest,
    xhs_profile_scrape, xhs_profiles_list, xhs_profile_detail, xhs_profile_videos,
)
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
from .views.checklist_views import ChecklistSubmitView, ChecklistCheckView, ChecklistSettingsView, ChecklistReportingStatusView
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
    path('ai/search', SearchView.as_view(), name='ai-search-compat'),
    path('ai/search/', SearchView.as_view(), name='ai-search-compat-slash'),
    path('search/status/<str:task_id>/', SearchStatusView.as_view(), name='search-status'),
    path('ai/search/status/<str:task_id>/', SearchStatusView.as_view(), name='ai-search-status-compat'),
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
    path('videos/clear-index/', clear_index, name='clear-index'),
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
    path('facebook/competitor-insights/', analyze_facebook_competitor, name='facebook-competitor-insights'),
    path('facebook/channel-metrics/', facebook_channel_metrics, name='facebook-channel-metrics'),
    path('facebook/import/', facebook_import, name='facebook-import'),
    path('facebook/sync/', facebook_sync, name='facebook-sync'),
    path('facebook/backfill/', facebook_backfill, name='facebook-backfill'),
    path('facebook/manage-pages/', get_managed_pages, name='facebook-pages-list'),
    path('facebook/page-videos/<str:page_id>/', get_synced_videos, name='facebook-videos-list'),

    # Scraper — All videos (gom tất cả nền tảng)
    path('scraper/all-videos/', all_external_videos, name='scraper-all-videos'),
    path('scraper/owned/videos/', owned_channel_videos, name='scraper-owned-videos'),

    # Scraper — Keyword Search, Discovery & Fanpages
    path('scraper/reels/search/', search_reels, name='scraper-reels-search'),
    path('scraper/keywords/suggest/', keyword_suggest, name='scraper-keyword-suggest'),
    path('scraper/keywords/hit/', keyword_hit, name='scraper-keyword-hit'),
    path('scraper/keywords/', keyword_list, name='scraper-keyword-list'),
    path('scraper/keywords/create/', keyword_create, name='scraper-keyword-create'),
    path('scraper/discover/', trigger_discovery, name='scraper-discover'),
    path('scraper/fanpages/', discovered_fanpages, name='scraper-fanpages'),
    path('scraper/fanpages/<int:fanpage_id>/', fanpage_detail, name='scraper-fanpage-detail'),
    path('scraper/fanpages/<int:fanpage_id>/toggle/', fanpage_toggle, name='scraper-fanpage-toggle'),
    path('scraper/fanpages/scrape-reels/', trigger_scrape_reels, name='scraper-scrape-reels'),
    path('scraper/fanpages/scrape-by-url/', fanpage_scrape_by_url, name='scraper-fanpage-scrape-by-url'),

    # TikTok (keyword search)
    path('scraper/tiktok/search/', tiktok_search, name='scraper-tiktok-search'),
    path('scraper/tiktok/videos/', tiktok_videos, name='scraper-tiktok-videos'),
    path('scraper/tiktok/keywords/suggest/', tiktok_keyword_suggest, name='scraper-tiktok-keyword-suggest'),

    # TikTok (profile posts)
    path('scraper/tiktok/profiles/', tiktok_profiles_list, name='scraper-tiktok-profiles'),
    path('scraper/tiktok/profiles/scrape/', tiktok_profile_scrape, name='scraper-tiktok-profile-scrape'),
    path('scraper/tiktok/profiles/<int:profile_id>/', tiktok_profile_detail, name='scraper-tiktok-profile-detail'),
    path('scraper/tiktok/profiles/<int:profile_id>/videos/', tiktok_profile_videos, name='scraper-tiktok-profile-videos'),
    path('scraper/tiktok/profiles/<int:profile_id>/toggle/', tiktok_profile_toggle, name='scraper-tiktok-profile-toggle'),

    # Instagram (profile reels)
    path('scraper/instagram/profiles/', instagram_profiles_list, name='scraper-instagram-profiles'),
    path('scraper/instagram/profiles/scrape/', instagram_profile_scrape, name='scraper-instagram-profile-scrape'),
    path('scraper/instagram/profiles/<int:profile_id>/', instagram_profile_detail, name='scraper-instagram-profile-detail'),
    path('scraper/instagram/profiles/<int:profile_id>/reels/', instagram_profile_reels, name='scraper-instagram-profile-reels'),
    path('scraper/instagram/profiles/<int:profile_id>/toggle/', instagram_profile_toggle, name='scraper-instagram-profile-toggle'),

    # Generic Channel Analysis (all platforms)
    path('channel/insights/', channel_insights_generic, name='channel-insights-generic'),
    path('channel/metrics/', channel_metrics_generic, name='channel-metrics-generic'),
    path('channel/analysis-unified/', channel_analysis_unified_generic, name='channel-analysis-unified'),
    
    # Douyin Search (cũ — real-time, không lưu DB)
    path('douyin/search/', search_douyin_videos, name='douyin-search'),
    # Douyin Scraper (mới — lưu DB, giống TikTok)
    path('scraper/douyin/search/', douyin_keyword_search, name='scraper-douyin-search'),
    path('scraper/douyin/videos/', douyin_videos_list, name='scraper-douyin-videos'),
    path('scraper/douyin/keywords/suggest/', douyin_keyword_suggest, name='scraper-douyin-keyword-suggest'),
    path('scraper/douyin/profile/scrape/', douyin_profile_scrape, name='scraper-douyin-profile-scrape'),
    path('scraper/douyin/profiles/', douyin_profiles_list, name='scraper-douyin-profiles'),
    path('scraper/douyin/profiles/<int:pk>/', douyin_profile_detail, name='scraper-douyin-profile-detail'),
    path('scraper/douyin/profiles/<int:pk>/toggle/', douyin_profile_toggle, name='scraper-douyin-profile-toggle'),
    path('scraper/douyin/profiles/<int:pk>/videos/', douyin_profile_videos, name='scraper-douyin-profile-videos'),

    # Douyin Channel Profile (full: followers, avatar, engagement) — called on Update only
    path('douyin/profile/', fetch_douyin_channel_profile, name='douyin-channel-profile'),
    
    # Xiaohongshu Search (TikHub)
    path('scraper/xiaohongshu/search/', search_xiaohongshu_notes, name='scraper-xiaohongshu-search'),
    path('scraper/xiaohongshu/videos/', list_xiaohongshu_videos, name='scraper-xiaohongshu-videos'),
    path('scraper/xiaohongshu/keywords/suggest/', xiaohongshu_keyword_suggest, name='scraper-xiaohongshu-keyword-suggest'),
    # Legacy (redirect-in-place: keep old path working)
    path('xiaohongshu/search/', search_xiaohongshu_notes, name='xiaohongshu-search'),

    # Xiaohongshu Profiles (TikHub)
    path('scraper/xiaohongshu/profiles/', xhs_profiles_list, name='scraper-xhs-profiles'),
    path('scraper/xiaohongshu/profiles/scrape/', xhs_profile_scrape, name='scraper-xhs-profile-scrape'),
    path('scraper/xiaohongshu/profiles/<int:profile_id>/', xhs_profile_detail, name='scraper-xhs-profile-detail'),
    path('scraper/xiaohongshu/profiles/<int:profile_id>/videos/', xhs_profile_videos, name='scraper-xhs-profile-videos'),
    
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
    path('checklist/settings/', ChecklistSettingsView.as_view(), name='checklist-settings'),
    path('checklist/status/', ChecklistReportingStatusView.as_view(), name='checklist-status'),
    
    # Collections (router URLs)
    path('', include(router.urls)),
]
