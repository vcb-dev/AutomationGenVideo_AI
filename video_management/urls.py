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
    # Stats views
    StatsView,
    HealthCheckView,
)
from .views.collection_views import VideoCollectionViewSet

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
    
    # Channel endpoints
    path('channels/', ChannelListCreateView.as_view(), name='channel-list'),
    path('channels/<int:pk>/', ChannelDetailView.as_view(), name='channel-detail'),
    path('channels/<int:pk>/check/', ChannelCheckView.as_view(), name='channel-check'),
    
    # Statistics
    path('stats/', StatsView.as_view(), name='stats'),
    
    # Collections (router URLs)
    path('', include(router.urls)),
]
