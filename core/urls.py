"""
URL configuration for core project.
"""
from django.contrib import admin
from django.urls import path, include
from rest_framework.views import APIView
from rest_framework.response import Response
# from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView


class APIRootView(APIView):
    """
    Root view that lists all available API endpoints
    """
    def get(self, request):
        return Response({
            'message': 'AutomationGenVideo AI Service v2.0',
            'status': 'running',
            'version': '2.0.0',
            'description': 'Multi-platform video scraping service',
            'platforms': ['tiktok', 'douyin', 'instagram', 'facebook'],
            'endpoints': {
                'health': {
                    'url': '/api/health/',
                    'method': 'GET',
                    'description': 'Health check endpoint'
                },
                'search': {
                    'url': '/api/search/',
                    'method': 'POST',
                    'description': 'Search videos across platforms',
                    'body': {
                        'platform': 'string (required): tiktok, douyin, instagram, facebook',
                        'keyword': 'string (required): search keyword or hashtag',
                        'min_likes': 'integer (optional, default: 0)',
                        'min_views': 'integer (optional, default: 0)',
                        'max_results': 'integer (optional, default: 20, max: 100)',
                        'use_cache': 'boolean (optional, default: true)',
                        'async_mode': 'boolean (optional, default: false)'
                    }
                },
                'search_status': {
                    'url': '/api/search/status/{task_id}/',
                    'method': 'GET',
                    'description': 'Check async search task status'
                },
                'search_history': {
                    'url': '/api/search/history/',
                    'method': 'GET',
                    'description': 'Get search history',
                    'params': {
                        'platform': 'string (optional): filter by platform',
                        'limit': 'integer (optional, default: 50)'
                    }
                },
                'user_videos': {
                    'url': '/api/search/user-videos/',
                    'method': 'POST',
                    'description': 'Get videos from specific user/channel',
                    'body': {
                        'platform': 'string (required)',
                        'username': 'string (required)',
                        'max_results': 'integer (optional, default: 20)'
                    }
                },
                'channels': {
                    'url': '/api/channels/',
                    'method': 'GET, POST',
                    'description': 'List or create tracked channels'
                },
                'channel_detail': {
                    'url': '/api/channels/{id}/',
                    'method': 'GET, PUT, DELETE',
                    'description': 'Retrieve, update or delete channel'
                },
                'channel_check': {
                    'url': '/api/channels/{id}/check/',
                    'method': 'POST',
                    'description': 'Manually trigger channel check'
                },
                'stats': {
                    'url': '/api/stats/',
                    'method': 'GET',
                    'description': 'Get system statistics and analytics'
                }
            },
            'documentation': '/api/docs/',
            'openapi_schema': '/api/schema/',
            'admin_panel': '/admin/'
        })


urlpatterns = [
    path('', APIRootView.as_view(), name='api-root'),
    path('admin/', admin.site.urls),
    # path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    # path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    # Trigger Reload Again for TikTok
    path('api/', include('video_management.urls')),
]

# VCB Assistant Chat
from video_management.views import chat_views
from video_management.views.analytics_chat_views import analytics_chat

urlpatterns += [
    path('api/chat/', chat_views.chat, name='vcb-chat'),
    path('api/chat/analytics/', analytics_chat, name='vcb-analytics-chat'),
]

# Cached audio serving endpoint
from video_management.views.smart_mix_video_views import serve_cached_audio

urlpatterns += [
    path('api/audio/cache/<str:filename>', serve_cached_audio, name='serve-cached-audio'),
]

# HeyGen lipsync/motion control endpoints
from heygen_service import views as heygen_views

urlpatterns += [
    path('api/heygen/generate-video', heygen_views.generate_video, name='heygen-generate'),
    path('api/heygen/generate-script', heygen_views.generate_script, name='heygen-generate-script'),
    path('api/heygen/generate-and-wait', heygen_views.generate_and_wait, name='heygen-generate-wait'),
    path('api/heygen/status/<str:video_id>', heygen_views.get_video_status, name='heygen-status'),
    path('api/heygen/voices', heygen_views.list_voices, name='heygen-voices'),
    path('api/heygen/clone-voice', heygen_views.clone_voice_from_video, name='heygen-clone-voice'),
    path('api/heygen/generate-audio', heygen_views.generate_audio, name='heygen-generate-audio'),
]

# Content Generation endpoints
from video_management.views import content_generation_views

urlpatterns += [
    path('api/content/generate/', content_generation_views.generate_content, name='content-generate'),
    path('api/content/generate-prompt/', content_generation_views.generate_prompt, name='content-generate-prompt'),
    path('api/content/video/<int:video_id>/', content_generation_views.get_generated_contents, name='content-by-video'),
    path('api/content/<int:content_id>/', content_generation_views.get_content_detail, name='content-detail'),
    path('api/ai/transform-content/', content_generation_views.transform_content, name='transform-content'),
]

# PAAST Content Analyzer — phân tích & chấm điểm content theo khung PAAST
from video_management.views import paast_analysis_views

urlpatterns += [
    path('api/ai/paast/analyze/', paast_analysis_views.analyze_content, name='paast-analyze'),
    path('api/ai/paast/analyze-v2/', paast_analysis_views.analyze_content_v2, name='paast-analyze-v2'),
    path('api/ai/paast/upgrade/', paast_analysis_views.upgrade_content, name='paast-upgrade'),
    # Nâng cấp kịch bản content-transform (giữ giọng nhân vật) — viết lại + chấm PAAST bản mới
    # trong CÙNG 1 request, xem transform_content_upgrade().
    path('api/ai/transform-content/upgrade/', paast_analysis_views.transform_content_upgrade, name='transform-content-upgrade'),
]

# Video Transcription (Speech-to-Text via OpenAI Whisper)
from video_management.views import transcribe_views

urlpatterns += [
    path('api/content/transcribe/', transcribe_views.transcribe_video, name='content-transcribe'),
    path('api/content/transcribe-upload/', transcribe_views.transcribe_upload, name='content-transcribe-upload'),
]

# Video Downloader (Tiện ích → Tải video, dùng yt-dlp)
from video_management.views import video_downloader_views

urlpatterns += [
    path('api/tools/video-downloader/info/', video_downloader_views.video_info, name='video-dl-info'),
    path('api/tools/video-downloader/jobs/', video_downloader_views.start_download, name='video-dl-start'),
    path('api/tools/video-downloader/jobs/<str:job_id>/', video_downloader_views.download_status, name='video-dl-status'),
    path('api/tools/video-downloader/jobs/<str:job_id>/file/', video_downloader_views.download_file, name='video-dl-file'),
]

# Chi tiết 1 video theo (platform, video_id) — làm giàu dữ liệu cho luồng đề xuất video.
from video_management.views import video_detail_views

urlpatterns += [
    path('api/scraper/video-detail/', video_detail_views.get_video_detail, name='scraper-video-detail'),
]

# Link phát trực tiếp — BE dùng để làm trung gian phát video ngay trên web hệ thống.
from video_management.views import play_url_views

urlpatterns += [
    path('api/scraper/play-url/', play_url_views.get_play_url, name='scraper-play-url'),
]

# Serve media files in development
from django.conf import settings
from django.conf.urls.static import static

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

