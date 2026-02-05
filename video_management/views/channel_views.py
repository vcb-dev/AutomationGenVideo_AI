import logging
import os
import tempfile
import requests
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
            # REMOVED FILTER: We want all videos
            filtered = normalized
            # filtered = [
            #     v for v in normalized
            #     if v.get('likes_count', 0) >= channel.min_likes_threshold
            # ]
            
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


class ChannelCheckByUsernameView(APIView):
    """
    Trigger check for a channel by username and platform.
    
    POST /api/channels/check-by-username/
    Body: { "username": "...", "platform": "..." }
    """
    
    def post(self, request):
        try:
            username = request.data.get('username')
            platform_str = request.data.get('platform')
            
            if not username or not platform_str:
                return Response(
                    {'error': 'username and platform are required'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Find channel
            try:
                platform_enum = Platform[platform_str.upper()]
                channel = TrackedChannel.objects.get(
                    username=username,
                    platform=platform_enum
                )
            except (KeyError, TrackedChannel.DoesNotExist):
                # Auto-create if not exists in Django DB (since it exists in NestJS)
                try:
                    platform_enum = Platform[platform_str.upper()]
                    channel = TrackedChannel.objects.create(
                        username=username,
                        platform=platform_enum,
                        channel_id=username, # Fallback ID
                        display_name=username
                    )
                except Exception as create_err:
                     return Response(
                        {'error': f'Channel not found in AI DB and could not create: {str(create_err)}'},
                        status=status.HTTP_404_NOT_FOUND
                    )
            
            logger.info(f"Checking channel by username: {channel.username} ({channel.platform})")
            
            # Run scraper
            # channel.platform is a string (e.g. 'tiktok'), no need for .value
            scraper = create_scraper(channel.platform)
            
            # Optimised fetch: Use only 1 result if just for checking existence/metadata
            # But here user wants to get new videos, so we fetch normal amount
            # FAST METADATA FETCH: Only get 5 recent videos for display
            # No download/fingerprinting here - will be done when viewing dashboard
            raw_results = scraper.get_user_videos(
                username=channel.username,
                max_results=5  # Only 5 for quick metadata
            )
            
            normalized = [scraper.normalize_video_data(v) for v in raw_results]
            
            # Save to DB (metadata only, no fingerprints)
            saved_videos = scraper.save_videos(normalized)
            channel.mark_checked()
            
            logger.info(f"✅ Channel added successfully: {channel.username}, fetched {len(saved_videos)} videos (metadata only)")
            
            return Response({
                'success': True,
                'message': f'Channel added: {channel.username}. Video analysis will run when you view the dashboard.',
                'channel_id': channel.id,
                'total_found': len(raw_results),
                'saved': len(saved_videos)
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Channel check error: {str(e)}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
