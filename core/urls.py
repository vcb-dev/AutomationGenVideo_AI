"""
URL configuration for core project.
"""
from django.contrib import admin
from django.urls import path, include
from rest_framework.views import APIView
from rest_framework.response import Response


class APIRootView(APIView):
    """
    Root view that lists all available API endpoints
    """
    def get(self, request):
        return Response({
            'message': 'AutomationGenVideo AI Service v2.0',
            'status': 'running',
            'version': '2.0.0',
            'description': 'Multi-platform video scraping service using Apify',
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
            'documentation': 'See README.md for detailed API documentation',
            'admin_panel': '/admin/'
        })


urlpatterns = [
    path('', APIRootView.as_view(), name='api-root'),
    path('admin/', admin.site.urls),
    path('api/', include('video_management.urls')),
]

# Serve media files in development
from django.conf import settings
from django.conf.urls.static import static

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
