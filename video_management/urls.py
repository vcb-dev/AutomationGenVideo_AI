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
from .views.voice_views import (
    list_voices_api, clone_voice_api, voice_tts_api,
    clone_voice_start_api, clone_voice_job_status_api,
)
from .views.facebook_fetch_views import (
    fetch_managed_pages, fetch_page_sync, fetch_page_backfill, fetch_metrics_refresh,
)
from .views.facebook_external_fetch_views import fetch_facebook_page_reels
from .views.douyin_fetch_views import fetch_douyin_search, fetch_douyin_profile_videos
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
from .views.xiaohongshu_fetch_views import fetch_xiaohongshu_search, fetch_xiaohongshu_profile_videos
from .views.tiktok_search_views import search_tiktok_videos
from .views.tiktok_suggest_views import tiktok_search_suggest
from .views.tiktok_fetch_views import fetch_tiktok_search, fetch_tiktok_profile_posts
from .views.instagram_fetch_views import fetch_instagram_profile_reels
from .views.youtube_fetch_views import fetch_youtube_channel
from .views.kuaishou_fetch_views import fetch_kuaishou_profile, fetch_kuaishou_search
from .views.bilibili_fetch_views import fetch_bilibili_profile, fetch_bilibili_search
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
from .views.task_script_views import generate_task_video_script, translate_task_video_script

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

    # Minimax Voice Cloning & TTS
    path('voice/list/', list_voices_api, name='voice-list'),
    path('voice/clone/', clone_voice_api, name='voice-clone'),
    path('voice/clone/start/', clone_voice_start_api, name='voice-clone-start'),
    path('voice/clone/status/<str:job_id>/', clone_voice_job_status_api, name='voice-clone-status'),
    path('voice/tts/', voice_tts_api, name='voice-tts'),


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
    # facebook/import, /sync, /backfill, /manage-pages, /page-videos giờ do BE xử lý
    # native (FacebookOwnedPagesController) — AI không còn route đọc nào cho owned pages.

    # Facebook owned-pages — fetch-only (BE sở hữu DB, gọi các endpoint này để lấy data thô)
    path('facebook/fetch/managed-pages/', fetch_managed_pages, name='facebook-fetch-managed-pages'),
    path('facebook/fetch/sync/', fetch_page_sync, name='facebook-fetch-sync'),
    path('facebook/fetch/backfill/', fetch_page_backfill, name='facebook-fetch-backfill'),
    path('facebook/fetch/metrics-refresh/', fetch_metrics_refresh, name='facebook-fetch-metrics-refresh'),

    # Scraper — All videos/owned videos (gom tất cả nền tảng) đã chuyển hẳn sang BE
    # (ScraperAggregateController) — đọc thẳng Prisma, không còn qua AI nữa.

    # Scraper — Keyword bookkeeping (autocomplete/hit tracking) đã chuyển sang BE
    # (SearchKeywordsController) — AI không còn đụng SearchKeyword nữa.

    # Fanpages — toggle/scrape-reels/scrape-by-url VÀ list/detail/reels-search đều đã
    # chuyển sang BE (FacebookExternalScraperController/FacebookReelsSearchController).

    # Fanpages — fetch-only (BE sở hữu DB, gọi endpoint này để lấy data thô)
    path('scraper/fanpages/fetch/reels/', fetch_facebook_page_reels, name='scraper-fanpages-fetch-reels'),

    # TikTok — search/profiles/scrape/toggle VÀ list/detail/videos đều đã chuyển sang BE
    # (TiktokScraperController) — chỉ còn fetch-only ở đây.
    path('scraper/tiktok/fetch/search/', fetch_tiktok_search, name='scraper-tiktok-fetch-search'),
    path('scraper/tiktok/fetch/profile-posts/', fetch_tiktok_profile_posts, name='scraper-tiktok-fetch-profile-posts'),

    # Instagram — profiles/scrape/toggle VÀ list/detail/reels đều đã chuyển sang BE
    # (InstagramScraperController) — chỉ còn fetch-only ở đây.
    path('scraper/instagram/fetch/profile-reels/', fetch_instagram_profile_reels, name='scraper-instagram-fetch-profile-reels'),

    # Generic Channel Analysis (all platforms)
    path('channel/insights/', channel_insights_generic, name='channel-insights-generic'),
    path('channel/metrics/', channel_metrics_generic, name='channel-metrics-generic'),
    path('channel/analysis-unified/', channel_analysis_unified_generic, name='channel-analysis-unified'),

    # Douyin — search/profile/scrape/toggle VÀ list/detail/videos đều đã chuyển sang BE
    # (DouyinScraperController) — chỉ còn fetch-only ở đây.
    path('scraper/douyin/fetch/search/', fetch_douyin_search, name='scraper-douyin-fetch-search'),
    path('scraper/douyin/fetch/profile-videos/', fetch_douyin_profile_videos, name='scraper-douyin-fetch-profile-videos'),

    # Xiaohongshu — search/profiles/scrape/toggle VÀ list/detail/videos đều đã chuyển
    # sang BE (XiaohongshuScraperController) — chỉ còn fetch-only ở đây.
    path('scraper/xiaohongshu/fetch/search/', fetch_xiaohongshu_search, name='scraper-xiaohongshu-fetch-search'),
    path('scraper/xiaohongshu/fetch/profile-videos/', fetch_xiaohongshu_profile_videos, name='scraper-xiaohongshu-fetch-profile-videos'),

    # YouTube — nền tảng mới, BE sở hữu DB hoàn toàn từ đầu (YoutubeScraperController)
    # — AI chỉ còn fetch-only ở đây.
    path('scraper/youtube/fetch/channel/', fetch_youtube_channel, name='scraper-youtube-fetch-channel'),

    # Kuaishou — nền tảng mới, chỉ có kênh ngoài (external), BE sở hữu DB hoàn
    # toàn từ đầu (KuaishouScraperController) — AI chỉ còn fetch-only ở đây.
    path('scraper/kuaishou/fetch/profile/', fetch_kuaishou_profile, name='scraper-kuaishou-fetch-profile'),
    path('scraper/kuaishou/fetch/search/', fetch_kuaishou_search, name='scraper-kuaishou-fetch-search'),

    # Bilibili — nền tảng mới, chỉ có kênh ngoài (external), BE sở hữu DB hoàn
    # toàn từ đầu (BilibiliScraperController) — AI chỉ còn fetch-only ở đây.
    path('scraper/bilibili/fetch/profile/', fetch_bilibili_profile, name='scraper-bilibili-fetch-profile'),
    path('scraper/bilibili/fetch/search/', fetch_bilibili_search, name='scraper-bilibili-fetch-search'),

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
    
    # Task-Auto: adapt content thắng cho sản phẩm mới (BE task-auto/tasks module)
    path('task-auto/video-script/generate/', generate_task_video_script, name='task-auto-video-script-generate'),
    path('task-auto/video-script/translate/', translate_task_video_script, name='task-auto-video-script-translate'),

    # Checklist công việc -> Lark Bitable
    path('checklist/check/', ChecklistCheckView.as_view(), name='checklist-check'),
    path('checklist/submit/', ChecklistSubmitView.as_view(), name='checklist-submit'),
    path('checklist/settings/', ChecklistSettingsView.as_view(), name='checklist-settings'),
    path('checklist/status/', ChecklistReportingStatusView.as_view(), name='checklist-status'),
    
    # Collections (router URLs)
    path('', include(router.urls)),
]
