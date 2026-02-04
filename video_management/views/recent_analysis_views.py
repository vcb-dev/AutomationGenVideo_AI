import logging
from datetime import datetime, timedelta
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from ..models import ScrapedVideo
from ..services.deep_learning_fingerprint_service import get_fingerprint_service
import os
import tempfile
import requests

logger = logging.getLogger(__name__)


@api_view(['POST'])
def analyze_recent_videos(request):
    """
    Fetch and fingerprint videos from last 7 days for a channel.
    Called when user views channel dashboard.
    
    POST /api/channels/analyze-recent/
    Body: { "channel_id": "..." }
    """
    try:
        channel_id = request.data.get('channel_id')
        
        if not channel_id:
            return Response(
                {'error': 'channel_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get videos from last 3 days without fingerprints (User requested reduction from 7 to 3)
        start_date = datetime.now() - timedelta(days=3)
        
        videos_to_process = ScrapedVideo.objects.filter(
            channel_id=channel_id,
            created_at__gte=start_date,
            feature_vector__isnull=True  # Only videos without fingerprints
        ).order_by('-created_at')[:20]  # Max 20 videos
        
        if not videos_to_process:
            return Response({
                'success': True,
                'message': 'All recent videos already analyzed',
                'analyzed': 0
            }, status=status.HTTP_200_OK)
        
        logger.info(f"Analyzing {len(videos_to_process)} recent videos...")
        
        fingerprint_service = get_fingerprint_service()
        success_count = 0
        failed_count = 0
        
        for vid in videos_to_process:
            try:
                logger.info(f"Processing video: {vid.video_id}")
                
                # Prepare URLs
                urls_to_try = []
                if vid.download_url:
                    urls_to_try.append(vid.download_url)
                
                raw_data = vid.raw_data if isinstance(vid.raw_data, dict) else {}
                if raw_data.get('videoUrl'):
                    urls_to_try.append(raw_data.get('videoUrl'))
                
                if vid.video_url:
                    urls_to_try.append(vid.video_url)
                
                urls_to_try = list(set(urls_to_try))
                
                download_success = False
                tmp_path = None
                
                # Try downloading from each URL
                for url in urls_to_try:
                    if download_success:
                        break
                    
                    try:
                        # Use yt-dlp for web URLs
                        if 'tiktok.com' in url and '/video/' in url:
                            import yt_dlp
                            logger.info(f"Extracting via yt-dlp: {url}")
                            
                            ydl_opts = {
                                'quiet': True,
                                'no_warnings': True,
                                'format': 'best[ext=mp4]/best',
                            }
                            
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                info = ydl.extract_info(url, download=False)
                                direct_url = info.get('url')
                                
                                if direct_url:
                                    url = direct_url
                        
                        # Download video
                        headers = {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            'Referer': 'https://www.tiktok.com/'
                        }
                        
                        with requests.get(url, stream=True, timeout=120, headers=headers) as r:
                            r.raise_for_status()
                            
                            tf = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
                            tmp_path = tf.name
                            
                            try:
                                for chunk in r.iter_content(chunk_size=8192):
                                    if chunk:
                                        tf.write(chunk)
                            finally:
                                tf.close()
                        
                        # Verify video
                        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1024:
                            import cv2
                            cap = cv2.VideoCapture(tmp_path)
                            if cap.isOpened():
                                ret, _ = cap.read()
                                if ret:
                                    download_success = True
                            cap.release()
                        
                        if not download_success and tmp_path and os.path.exists(tmp_path):
                            os.unlink(tmp_path)
                    
                    except Exception as e:
                        logger.warning(f"Failed to download from {url}: {e}")
                        if tmp_path and os.path.exists(tmp_path):
                            os.unlink(tmp_path)
                
                # Extract fingerprint if download successful
                if download_success and tmp_path:
                    try:
                        features = fingerprint_service.extract_features(tmp_path)
                        vid.feature_vector = features.tobytes()
                        vid.save()
                        
                        success_count += 1
                        logger.info(f"✅ Fingerprinted: {vid.video_id}")
                    
                    finally:
                        if os.path.exists(tmp_path):
                            os.unlink(tmp_path)
                else:
                    failed_count += 1
                    logger.warning(f"❌ Failed to download: {vid.video_id}")
            
            except Exception as e:
                failed_count += 1
                logger.error(f"Error processing {vid.video_id}: {e}")
        
        return Response({
            'success': True,
            'message': f'Analysis complete',
            'analyzed': success_count,
            'failed': failed_count,
            'total': len(videos_to_process)
        }, status=status.HTTP_200_OK)
    
    except Exception as e:
        logger.error(f"Recent video analysis error: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
