"""
Facebook Analysis Views - Using Hybrid Service (Apify).

Provides REST API endpoints for analyzing Facebook Pages and User Profiles.
"""

import logging
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.csrf import csrf_exempt

from ..services.facebook_hybrid_service import get_facebook_hybrid_service

logger = logging.getLogger(__name__)


@csrf_exempt
@api_view(['POST'])
def analyze_facebook_url(request):
    """
    Analyze a Facebook URL (Page or User Profile).
    
    POST /api/facebook/analyze
    Body:
    {
        "url": "https://www.facebook.com/...",
        "max_posts": 20,  // optional, default 20
        "force_method": "auto"  // optional: "auto", "graph", "apify"
    }
    
    Response:
    {
        "success": true,
        "data": {
            "type": "page" | "profile" | "group",
            "method": "graph_api" | "apify",
            "name": "Page Name",
            "identifier": "page_id",
            "followers_count": 10000 | null,
            "posts_count": 500,
            "posts": [...],
            "metadata": {...}
        }
    }
    """
    try:
        # Get request data
        url = request.data.get('url')
        max_posts = request.data.get('max_posts', 20)
        force_method = request.data.get('force_method', 'auto')
        
        # Validate
        if not url:
            return Response({
                'success': False,
                'error': 'URL is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate force_method
        if force_method not in ['auto', 'graph', 'apify']:
            force_method = 'auto'
        
        # Convert 'auto' to None for service
        method = None if force_method == 'auto' else force_method
        
        logger.info(f"📊 Analyzing Facebook URL: {url}")
        logger.info(f"   Max posts: {max_posts}, Method: {force_method}")
        
        # Get service and analyze
        service = get_facebook_hybrid_service()
        result = service.get_facebook_data(
            url=url,
            max_posts=max_posts,
            force_method=method
        )
        
        logger.info(f"✅ Analysis complete: {result['type']} - {result['name']}")
        
        return Response({
            'success': True,
            'data': result
        }, status=status.HTTP_200_OK)
        
    except ValueError as e:
        logger.error(f"❌ Validation error: {str(e)}")
        return Response({
            'success': False,
            'error': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)
        
    except Exception as e:
        logger.error(f"❌ Analysis failed: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'error': f"Failed to analyze Facebook URL: {str(e)}"
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(['GET'])
def get_available_methods(request):
    """
    Get available analysis methods.
    
    GET /api/facebook/methods
    
    Response:
    {
        "success": true,
        "data": {
            "graph_api": true | false,
            "apify": true | false
        }
    }
    """
    try:
        service = get_facebook_hybrid_service()
        methods = service.get_available_methods()
        
        return Response({
            'success': True,
            'data': methods
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"❌ Failed to get methods: {str(e)}")
        return Response({
            'success': False,
            'error': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(['POST'])
def detect_facebook_type(request):
    """
    Detect if URL is a Page, Group, or User Profile.
    
    POST /api/facebook/detect
    Body:
    {
        "url": "https://www.facebook.com/..."
    }
    
    Response:
    {
        "success": true,
        "data": {
            "type": "page" | "profile" | "group",
            "identifier": "page_id_or_username"
        }
    }
    """
    try:
        url = request.data.get('url')
        
        if not url:
            return Response({
                'success': False,
                'error': 'URL is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        service = get_facebook_hybrid_service()
        fb_type, identifier = service.detect_facebook_type(url)
        
        return Response({
            'success': True,
            'data': {
                'type': fb_type,
                'identifier': identifier
            }
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"❌ Detection failed: {str(e)}")
        return Response({
            'success': False,
            'error': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
