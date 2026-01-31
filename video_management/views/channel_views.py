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
from ..services.deep_learning_fingerprint_service import get_fingerprint_service

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
            raw_results = scraper.get_user_videos(
                username=channel.username,
                max_results=30 # Fetch reasonable amount
            )
            
            normalized = [scraper.normalize_video_data(v) for v in raw_results]
            
            filtered = normalized
            # REMOVED FILTER: We want ALL videos for duplicate detection, regardless of likes.
            # filtered = [
            #     v for v in normalized
            #     if v.get('likes_count', 0) >= channel.min_likes_threshold
            # ]
            
            saved_videos = scraper.save_videos(filtered)
            channel.mark_checked()

            # --- DEEP ANALYSIS: Download & Fingerprint Top 5 Videos ---
            # This enables Content-Based Duplicate Detection even if titles differ.
            try:
                # PROCESS ALL VIDEOS for Deep Analysis (as requested by User)
                # Warning: This might be slow for large channels, but ensures accuracy.
                videos_to_process = saved_videos 
                
                if videos_to_process:
                    logger.info(f"Performing Deep Analysis on ALL {len(videos_to_process)} fetched videos...")
                    fingerprint_service = get_fingerprint_service()
                    
                    for vid in videos_to_process:
                        # Skip if already has vector
                        if vid.feature_vector:
                            continue
                            
                        try:
                            logger.info(f"Downloading video for analysis: {vid.video_id}")
                            
                            # 1. Prepare URLs
                            urls_to_try = []
                            # Prefer direct download links if available
                            if vid.download_url: urls_to_try.append(vid.download_url)
                            raw_data = vid.raw_data if isinstance(vid.raw_data, dict) else {}
                            if raw_data.get('videoUrl'): urls_to_try.append(raw_data.get('videoUrl'))
                            
                            # Fallback: Web URL (requires extraction)
                            if vid.video_url: urls_to_try.append(vid.video_url)
                            
                            # Deduplicate
                            urls_to_try = list(set(urls_to_try))
                            
                            download_success = False
                            tmp_path = None
                            
                            import yt_dlp

                            for url in urls_to_try:
                                if download_success: break
                                
                                target_url = url
                                is_web_url = 'tiktok.com' in url and '/video/' in url
                                
                                try:
                                    logger.debug(f"Processing URL: {url}")
                                    
                                    # If it's a Web URL, we MUST extract the real video link first
                                    if is_web_url:
                                        try:
                                            logger.info(f"Extracting direct link via yt-dlp for: {url}")
                                            ydl_opts = {
                                                'quiet': True,
                                                'no_warnings': True,
                                                'format': 'best[ext=mp4]/best',
                                            }
                                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                                info = ydl.extract_info(url, download=False)
                                                target_url = info.get('url')
                                                if not target_url:
                                                    raise ValueError("yt-dlp could not extract URL")
                                                logger.info("✅ Extracted direct video link successfully")
                                        except Exception as ydl_err:
                                            logger.warning(f"yt-dlp extraction failed: {ydl_err}")
                                            continue # Skip to next URL if extraction failed
                                    
                                    # Stream download to temp file with headers
                                    headers = {
                                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                                        'Referer': 'https://www.tiktok.com/'
                                    }
                                    
                                    with requests.get(target_url, stream=True, timeout=120, headers=headers) as r:
                                        r.raise_for_status()
                                        
                                        # Create temp file
                                        tf = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
                                        tmp_path = tf.name
                                        
                                        try:
                                            for chunk in r.iter_content(chunk_size=8192):
                                                if chunk:
                                                    tf.write(chunk)
                                        finally:
                                            tf.close()

                                    # Check if file is valid
                                    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1024:
                                        # Verify integrity with OpenCV
                                        import cv2
                                        cap = cv2.VideoCapture(tmp_path)
                                        if cap.isOpened():
                                            # Read one frame to be sure
                                            ret, _ = cap.read()
                                            if ret:
                                                download_success = True
                                        cap.release()
                                    
                                    if not download_success:
                                        logger.warning(f"Downloaded file invalid/unreadable from {target_url}")
                                        if tmp_path and os.path.exists(tmp_path): os.unlink(tmp_path)
                                        
                                except Exception as e:
                                    logger.warning(f"Download/Processing failed for {url}: {e}")
                                    if tmp_path and os.path.exists(tmp_path): os.unlink(tmp_path)

                            if download_success and tmp_path:
                                # Extract features
                                features = fingerprint_service.extract_features(tmp_path)
                                vid.feature_vector = features.tobytes() # Store as bytes in BinaryField
                                vid.save()
                                logger.info(f"✅ Extracted & Saved vector for {vid.video_id}")
                                
                                # Clean up
                                if os.path.exists(tmp_path):
                                   os.unlink(tmp_path)
                            else:
                                logger.error(f"Failed to obtain valid video for {vid.video_id} from any source.")
                                
                        except Exception as dl_err:
                            logger.error(f"Failed to deep analyze {vid.video_id}: {dl_err}")
                            if 'tmp_path' in locals() and tmp_path and os.path.exists(tmp_path):
                                os.unlink(tmp_path)
            except Exception as deep_err:
                logger.error(f"Deep Analysis failed: {deep_err}")
            # -----------------------------------------------------------
            
            return Response({
                'success': True,
                'message': f'Check completed for channel: {channel.username}',
                'channel_id': channel.id,
                'total_found': len(raw_results),
                'above_threshold': len(filtered),
                'saved': len(saved_videos)
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Channel check error: {str(e)}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
