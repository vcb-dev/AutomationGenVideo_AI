from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
import logging
from ..services.xiaohongshu_scraper import XiaohongshuScraperService

logger = logging.getLogger(__name__)

@api_view(['POST'])
@permission_classes([AllowAny])
def search_xiaohongshu_notes(request):
    """
    Search Xiaohongshu notes using Apify service.
    """
    try:
        data = request.data
        search_term = data.get('searchTerm', '').strip()
        sort_type = data.get('sortType', 'general')
        max_posts = int(data.get('maxPosts', 20))
        
        # Validation
        if not search_term:
            return Response(
                {'success': False, 'error': 'Search term is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        if max_posts > 100:
            max_posts = 100
            
        logger.info(f"Xiaohongshu search request - Term: {search_term}, Max: {max_posts}")
        
        # Initialize service
        scraper = XiaohongshuScraperService()
        
        # Perform search
        notes = scraper.search_notes(
            search_term=search_term,
            sort_type=sort_type,
            max_posts=max_posts
        )
        
        return Response({
            'success': True,
            'data': {
                'notes': notes,
                'total': len(notes),
                'has_more': False # Apify one-shot usually
            }
        })
        
    except Exception as e:
        logger.error(f"Xiaohongshu search failed: {str(e)}", exc_info=True)
        return Response(
            {'success': False, 'error': str(e)}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
