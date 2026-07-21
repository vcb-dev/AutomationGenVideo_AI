"""
TikTok Search Views - deprecated, kept for backward compatibility.
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
    NOTE: deprecated. Use the main /api/search/ endpoint with platform=tiktok instead.
    """
    return Response({
        'success': False,
        'error': 'This endpoint is deprecated. Please use /api/search/ with platform=tiktok instead.',
        'redirect': '/api/search/'
    }, status=status.HTTP_410_GONE)
