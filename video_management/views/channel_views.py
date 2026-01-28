"""
API views for channel tracking functionality.
"""

import logging
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import generics

from ..models import TrackedChannel, Platform, ScrapedVideo
from ..serializers import TrackedChannelSerializer
from ..services.apify_service import create_scraper

logger = logging.getLogger(__name__)


class ChannelListCreateView(generics.ListCreateAPIView):
    """
    List all tracked channels or create a new one.
    
    GET /api/channels/
    POST /api/channels/
    """
    
    queryset = TrackedChannel.objects.all()
    serializer_class = TrackedChannelSerializer
    
    def get_queryset(self):
        """Filter channels by query parameters."""
        queryset = super().get_queryset()
        
        platform = self.request.query_params.get('platform')
        is_active = self.request.query_params.get('is_active')
        
        if platform:
            try:
                platform_enum = Platform[platform.upper()]
                queryset = queryset.filter(platform=platform_enum)
            except KeyError:
                pass
        
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        return queryset.order_by('-created_at')


class ChannelDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    Retrieve, update or delete a tracked channel.
    
    GET /api/channels/{id}/
    PUT /api/channels/{id}/
    DELETE /api/channels/{id}/
    """
    
    queryset = TrackedChannel.objects.all()
    serializer_class = TrackedChannelSerializer


class ChannelCheckView(APIView):
    """
    Manually trigger check for a channel.
    
    POST /api/channels/{id}/check/
    """
    
    def post(self, request, pk):
        """Trigger channel check - now runs synchronously."""
        try:
            channel = TrackedChannel.objects.get(pk=pk)
            
            if not channel.is_active:
                return Response(
                    {'error': 'Channel is inactive'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            logger.info(f"Checking channel: {channel.username} ({channel.platform})")
            
            # Run synchronously instead of async
            scraper = create_scraper(channel.platform.value)
            raw_results = scraper.get_user_videos(
                username=channel.username,
                max_results=50
            )
            
            # Normalize results
            normalized = [scraper.normalize_video_data(v) for v in raw_results]
            
            # Filter by likes threshold
            filtered = [
                v for v in normalized
                if v.get('likes_count', 0) >= channel.min_likes_threshold
            ]
            
            # Save videos
            saved_videos = scraper.save_videos(filtered)
            
            # Update channel
            channel.mark_checked()
            
            logger.info(
                f"Channel check completed: {channel.username}, "
                f"found {len(filtered)} videos above threshold"
            )
            
            return Response({
                'success': True,
                'message': f'Check completed for channel: {channel.username}',
                'channel_id': channel.id,
                'total_found': len(raw_results),
                'above_threshold': len(filtered),
                'saved': len(saved_videos)
            }, status=status.HTTP_200_OK)
            
        except TrackedChannel.DoesNotExist:
            return Response(
                {'error': 'Channel not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Channel check error: {str(e)}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
