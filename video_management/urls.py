from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import TrackedChannelViewSet, SearchView, SearchStatusView, DownloadView, MusicPostsView, TestSearchView, ProxyImageView, ProxyVideoView

router = DefaultRouter()
router.register(r'channels', TrackedChannelViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('search', SearchView.as_view(), name='search'),
    path('search/status/<str:task_id>', SearchStatusView.as_view(), name='search-status'),
    path('search/test', TestSearchView.as_view(), name='search-test'),
    path('music/posts', MusicPostsView.as_view(), name='music-posts'),
    path('download', DownloadView.as_view(), name='download'),
    path('proxy/image', ProxyImageView.as_view(), name='proxy-image'),
    path('proxy/video', ProxyVideoView.as_view(), name='proxy-video'),
]
