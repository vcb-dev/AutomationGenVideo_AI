"""
API views for statistics and analytics.
"""

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Count, Q

from ..models import ScrapedVideo, SearchHistory, TrackedChannel, Platform
from ..serializers import StatsSerializer, VideoSerializer, SearchHistorySerializer

logger = logging.getLogger(__name__)


class StatsView(APIView):
    """
    Get general statistics and analytics.
    
    GET /api/stats/
    """
    
    def get(self, request):
        """Get system statistics."""
        try:
            # Count totals
            total_videos = ScrapedVideo.objects.count()
            total_searches = SearchHistory.objects.count()
            total_channels = TrackedChannel.objects.count()
            
            # Videos by platform
            videos_by_platform = {}
            for platform in Platform:
                count = ScrapedVideo.objects.filter(platform=platform).count()
                if count > 0:
                    videos_by_platform[platform.value] = count
            
            # Searches by platform
            searches_by_platform = {}
            for platform in Platform:
                count = SearchHistory.objects.filter(platform=platform).count()
                if count > 0:
                    searches_by_platform[platform.value] = count
            
            # Top videos by engagement
            top_videos = ScrapedVideo.objects.order_by(
                '-likes_count',
                '-views_count'
            )[:10]
            
            # Recent searches
            recent_searches = SearchHistory.objects.order_by('-created_at')[:10]
            
            # Serialize
            data = {
                'total_videos': total_videos,
                'total_searches': total_searches,
                'total_channels': total_channels,
                'videos_by_platform': videos_by_platform,
                'searches_by_platform': searches_by_platform,
                'top_videos': VideoSerializer(top_videos, many=True).data,
                'recent_searches': SearchHistorySerializer(recent_searches, many=True).data,
            }
            
            serializer = StatsSerializer(data)
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Stats error: {str(e)}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class HealthCheckView(APIView):
    """
    Health check endpoint.
    
    GET /api/health/
    """
    
    def get(self, request):
        """Simple health check."""
        return Response({
            'status': 'healthy',
            'service': 'AutomationGenVideo_AI',
            'version': '2.0.0'
        }, status=status.HTTP_200_OK)
