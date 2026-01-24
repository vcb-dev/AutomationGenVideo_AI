"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
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
            'message': 'Django Backend API is running!',
            'version': '1.0.0',
            'endpoints': {
                'admin': '/admin/',
                'api': {
                    'channels': '/api/channels/',
                    'search': {
                        'url': '/api/search',
                        'method': 'POST',
                        'description': 'Search videos by keyword',
                        'body': {
                            'keyword': 'string (required)',
                            'min_likes': 'integer (optional)',
                            'min_views': 'integer (optional)',
                            'sort_by': 'string (optional): likes, views'
                        }
                    },
                    'search_status': {
                        'url': '/api/search/status/<task_id>',
                        'method': 'GET',
                        'description': 'Check search task status'
                    },
                    'music_posts': {
                        'url': '/api/music/posts',
                        'method': 'POST',
                        'description': 'Get posts by music ID',
                        'body': {
                            'music_id': 'string (required)',
                            'count': 'integer (optional, default: 30)',
                            'cursor': 'integer (optional, default: 0)'
                        }
                    },
                    'download': {
                        'url': '/api/download',
                        'method': 'POST',
                        'description': 'Get download URL for video',
                        'body': {
                            'url': 'string (required): video URL'
                        }
                    }
                }
            },
            'documentation': 'Visit /api/ endpoints for API usage'
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
