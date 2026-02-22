"""
TikTok Search Views - Uses Apify scraper (same as other platforms).
TikHub has been removed from the project.
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
import logging

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([AllowAny])
def search_tiktok_videos(request):
    """
    Search TikTok videos.
    NOTE: TikHub removed. Use the main /api/search/ endpoint with platform=tiktok instead.
    This endpoint is kept for backward compatibility but redirects to the Apify-based search.
    """
    return Response({
        'success': False,
        'error': 'This endpoint is deprecated. Please use /api/search/ with platform=tiktok instead.',
        'redirect': '/api/search/'
    }, status=status.HTTP_410_GONE)
