from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from django.core.cache import cache
import logging

from ..serializers import MusicPostsSerializer

logger = logging.getLogger(__name__)

class MusicPostsView(APIView):
    """
    View to retrieve posts by musicId using Tikhub API
    """
    def post(self, request):
        serializer = MusicPostsSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            music_id = serializer.validated_data['music_id']
            count = serializer.validated_data.get('count', 30)
            cursor = serializer.validated_data.get('cursor', 0)
            min_likes = serializer.validated_data.get('min_likes', 0) or 0
            min_views = serializer.validated_data.get('min_views', 0) or 0
            
            # Check cache
            cache_key = f"music_posts_{music_id}_{count}_{cursor}_{min_likes}_{min_views}"
            cached_videos = cache.get(cache_key)
            
            if cached_videos:
                logger.info(f"Cache hit for {cache_key}")
                return Response({
                    'videos': cached_videos,
                    'count': len(cached_videos),
                    'music_id': music_id
                }, status=status.HTTP_200_OK)
            
            # Call API
            # Call API
            # from ..services.rapidapi_service import TikhubService
            
            # logger.info(f"Calling Tikhub API for musicId: {music_id}")
            # api_service = TikhubService()
            # videos = api_service.get_music_posts(...)
            
            # Temporarily disabled — no provider configured
            logger.warning("Music search disabled (no provider configured)")
            videos = []
            
            # Cache results
            # if videos:
            #     cache.set(cache_key, videos, timeout=1800)  # 30 minutes
            
            return Response({
                'videos': videos,
                'count': len(videos),
                'music_id': music_id
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Music posts view error: {e}", exc_info=True)
            return Response(
                {"error": f"An error occurred: {str(e)}"}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
