"""
Douyin Search Views

API endpoints for searching Douyin videos by keyword or hashtag.
Real-time search without database persistence.
"""

import logging
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from ..services.douyin_scraper import DouyinScraperService

logger = logging.getLogger(__name__)


@api_view(['POST'])
def search_douyin_videos(request):
    """
    Search Douyin videos by keyword or hashtag.
    
    POST /api/douyin/search
    
    Request Body:
    {
        "searchTerm": "美食",
        "searchType": "keyword",  // "keyword" or "hashtag"
        "maxPosts": 50,
        "sortBy": "general",  // "general", "most_liked", "latest"
        "publishTime": "all"  // "all", "last_day", "last_week", "last_half_year"
    }
    
    Response:
    {
        "success": true,
        "data": {
            "videos": [...],
            "total": 50,
            "searchTerm": "美食",
            "searchType": "keyword"
        }
    }
    """
    try:
        # Extract parameters
        search_term = request.data.get('searchTerm', '').strip()
        search_type = request.data.get('searchType', 'keyword')
        max_posts = int(request.data.get('maxPosts', 50))
        sort_by = request.data.get('sortBy', 'general')
        publish_time = request.data.get('publishTime', 'all')
        
        # Validation
        if not search_term:
            return Response({
                'success': False,
                'error': 'searchTerm is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if search_type not in ['keyword', 'hashtag']:
            return Response({
                'success': False,
                'error': 'searchType must be "keyword" or "hashtag"'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if sort_by not in ['general', 'most_liked', 'latest']:
            return Response({
                'success': False,
                'error': 'sortBy must be "general", "most_liked", or "latest"'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if publish_time not in ['all', 'last_day', 'last_week', 'last_half_year']:
            return Response({
                'success': False,
                'error': 'publishTime must be "all", "last_day", "last_week", or "last_half_year"'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Limit max_posts to reasonable value
        if max_posts > 100:
            max_posts = 100
        elif max_posts < 1:
            max_posts = 50
        
        logger.info(
            f"Douyin search request - Term: {search_term}, "
            f"Type: {search_type}, Max: {max_posts}"
        )
        
        # Initialize scraper
        scraper = DouyinScraperService()
        
        # Search videos
        videos = scraper.search_videos(
            search_term=search_term,
            search_type=search_type,
            max_posts=max_posts,
            sort_by=sort_by,
            publish_time=publish_time
        )
        
        logger.info(f"Successfully retrieved {len(videos)} Douyin videos")
        
        return Response({
            'success': True,
            'data': {
                'videos': videos,
                'total': len(videos),
                'searchTerm': search_term,
                'searchType': search_type,
                'sortBy': sort_by,
                'publishTime': publish_time
            }
        }, status=status.HTTP_200_OK)
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return Response({
            'success': False,
            'error': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)
        
    except Exception as e:
        logger.error(f"Douyin search failed: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'error': f'Search failed: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
