from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
import logging
from ..services.tikhub_service import get_tikhub_service

logger = logging.getLogger(__name__)

@api_view(['POST'])
@permission_classes([AllowAny])
def search_tiktok_videos(request):
    """
    Search TikTok videos using TikHub API.
    """
    try:
        data = request.data
        search_term = data.get('searchTerm', '').strip()
        search_type = data.get('searchType', 'keyword')
        sort_type = data.get('sortType', 0)
        cursor = int(data.get('cursor', 0))
        
        # Validation
        if not search_term:
            return Response(
                {'success': False, 'error': 'Search term is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Map sort types to TikHub expected values
        # Frontend: 'general' | 'hot' | 'latest'
        # TikHub: 0=general, 1=most_liked, 2=latest (assuming)
        if isinstance(sort_type, str):
            if sort_type == 'hot': sort_type = 1
            elif sort_type == 'latest': sort_type = 2
            else: sort_type = 0
            
        logger.info(f"TikTok search request - Term: {search_term}, Type: {search_type}, Cursor: {cursor}")
        
        # Initialize service
        service = get_tikhub_service()
        
        # Perform search
        result = service.search_videos(
            keyword=search_term,
            search_type=search_type,
            sort_type=int(sort_type),
            cursor=cursor
        )
        
        # Normalize result for frontend
        videos = []
        for item in result['videos']:
            try:
                # TikHub search result structure might vary slightly from user post structure
                # We extract core fields needed for UI
                video_info = {
                    'video_id': item.get('id') or item.get('video', {}).get('id', ''),
                    'desc': item.get('desc', ''),
                    'author_name': item.get('author', {}).get('nickname', ''),
                    'author_id': item.get('author', {}).get('uniqueId', ''),
                    'author_avatar': item.get('author', {}).get('avatarThumb', ''),
                    'stats': {
                        'digg_count': item.get('stats', {}).get('diggCount', 0),
                        'play_count': item.get('stats', {}).get('playCount', 0),
                        'comment_count': item.get('stats', {}).get('commentCount', 0),
                        'share_count': item.get('stats', {}).get('shareCount', 0),
                    },
                    'video': {
                        'cover': item.get('video', {}).get('cover', ''),
                        'play_addr': item.get('video', {}).get('playAddr', ''),
                        'duration': item.get('video', {}).get('duration', 0),
                    },
                    'music': {
                        'title': item.get('music', {}).get('title', ''),
                        'author': item.get('music', {}).get('authorName', ''),
                        'cover': item.get('music', {}).get('coverThumb', ''),
                    },
                    'create_time': item.get('createTime', 0),
                    'hashtags': [challenge.get('title', '') for challenge in item.get('challenges', [])] if item.get('challenges') else []
                }
                videos.append(video_info)
            except Exception as e:
                logger.warning(f"Failed to normalize TikTok video item: {e}")
                continue

        return Response({
            'success': True,
            'data': {
                'videos': videos,
                'cursor': result['cursor'],
                'has_more': result['has_more']
            }
        })
        
    except Exception as e:
        logger.error(f"TikTok search failed: {str(e)}", exc_info=True)
        return Response(
            {'success': False, 'error': str(e)}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
