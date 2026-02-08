"""
Smart Mix Video Views - NEW High-Performance Mix Solution

This module uses Smart Pre-processing approach:
- Index videos once (fast metadata scan)
- Generate clips on-demand with caching
- Mix using concat (copy codec) for speed

Performance: 5-13 seconds per mix (vs 2-3 minutes with old approach)
"""

import os
import uuid
import logging
import tempfile
import subprocess
import threading
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

from django.conf import settings
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from video_management.services.smart_preprocessing_service import get_preprocessing_service
from video_management.models import IndexedVideo, VideoClipCache

logger = logging.getLogger(__name__)

# Mix progress tracking (same as old mix_video_views.py)
_mix_progress = {}
_mix_progress_lock = threading.Lock()

# 10-slot formula folder types
FOLDER_TYPES = [
    "Sản phẩm",         # 0 - Slot đầu
    "HuyK",             # 1
    "Chế tác Above 1",  # 2 - Above đầu tiên
    "Chế tác Below 1",  # 3 - Below đầu tiên
    "Chế tác Above 2",  # 4 - Above thứ hai
    "HuyK Above 1",     # 5 - HuyK Above đầu
    "HuyK Above 2",     # 6 - HuyK Above thứ hai
    "Chế tác Below 2",  # 7 - Below thứ hai
    "Sản phẩm HT",      # 8 - Sản phẩm hoàn thiện
    "Outtrol",          # 9 - Slot cuối
]

# ============================================================================
# CÔNG THỨC A4 - Tiêu chuẩn cấu trúc video edit (11 slots, ~48s total)
# ============================================================================
# Reference: gen-n-CongThucA4.jpg
# 
# Cấu trúc timeline:
# 
#   ┌─────────┬─────┬─────────┬─────┬─────────┬──────────────────────┬──────────┬─────────┐
#   │ Sản phẩm│ HuyK│ Chế tác │ HuyK│ Chế tác │  Source góc + Logo   │ SP hoàn  │ Outtrol │
#   │  (Intro)│     │ Above 1 │     │ Below 1 │  (4 góc khác nhau)   │ thiện    │ (Outro) │
#   └─────────┴─────┴─────────┴─────┴─────────┴──────────────────────┴──────────┴─────────┘
#      5s       4s      5s       4s      5s          3s + 3s + 3s + 3s      7s        5s
#
# Chi tiết từng slot:
# 1. Sản phẩm (5s)        - Video sưu tầm + Tự quay: Giới thiệu sản phẩm ban đầu
# 2. HuyK (4s)            - Video người tạo/KOC
# 3. Chế tác Above 1 (5s) - Góc chụp trên: Quá trình chế tác lần 1
# 4. HuyK (4s)            - Video người tạo/KOC lần 2
# 5. Chế tác Below 1 (5s) - Góc chụp dưới: Quá trình chế tác lần 1
# 6. Chế tác Above 2 (3s) - Source góc 1 (với Tag logo HuyK)
# 7. HuyK Above 1 (3s)    - Source góc 2 (với Tag logo HuyK)
# 8. HuyK Above 2 (3s)    - Source góc 3 (với Tag logo HuyK)
# 9. Chế tác Below 2 (3s) - Source góc 4 (với Tag logo HuyK)
# 10. Sản phẩm HT (7s)    - Sản phẩm hoàn thiện (5-10s range)
# 11. Outtrol (5s)        - Outro HuyK/Brand ending
#
# Lưu ý: 
# - Nếu audio dài hơn 48s, toàn bộ 11 slots sẽ được lặp lại cho đến khi >= audio duration
# - Mỗi lần generate 5 videos sẽ chọn ngẫu nhiên videos khác nhau từ mỗi folder_type
# ============================================================================

A4_FORMULA = [
    {"folder_type": "Sản phẩm", "duration": 5},        # Slot 1: Intro
    {"folder_type": "HuyK", "duration": 4},            # Slot 2: HuyK
    {"folder_type": "Chế tác Above 1", "duration": 5}, # Slot 3: Chế tác Above
    {"folder_type": "HuyK", "duration": 4},            # Slot 4: HuyK
    {"folder_type": "Chế tác Below 1", "duration": 5}, # Slot 5: Chế tác Below
    {"folder_type": "Chế tác Above 2", "duration": 3}, # Slot 6: Source góc 1
    {"folder_type": "HuyK Above 1", "duration": 3},    # Slot 7: Source góc 2
    {"folder_type": "HuyK Above 2", "duration": 3},    # Slot 8: Source góc 3
    {"folder_type": "Chế tác Below 2", "duration": 3}, # Slot 9: Source góc 4
    {"folder_type": "Sản phẩm HT", "duration": 7},     # Slot 10: Sản phẩm hoàn thiện
    {"folder_type": "Outtrol", "duration": 5},         # Slot 11: Outro
]


@api_view(['POST'])
def index_folders(request):
    """
    Index videos from folders into database (one-time setup).
    
    POST Body:
    {
        "folders": {
            "Sản phẩm": "\\\\VCB_MEDIA\\...",
            "HuyK": "\\\\VCB_MEDIA\\...",
            ...
        },
        "videos_per_folder": 100  // Max videos to index per folder (0 = unlimited)
    }
    
    Returns:
    {
        "success": true,
        "results": {"Sản phẩm": 50, "HuyK": 30, ...},
        "total_indexed": 80
    }
    """
    try:
        data = request.data
        folders = data.get('folders', {})
        videos_per_folder = data.get('videos_per_folder', 0)
        
        if not folders:
            return Response(
                {'error': 'folders dict is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        service = get_preprocessing_service()
        results = service.index_videos_from_folders(folders, videos_per_folder)
        
        total = sum(results.values())
        
        return Response({
            'success': True,
            'results': results,
            'total_indexed': total,
            'message': f'Indexed {total} videos from {len(folders)} folders'
        })
    
    except Exception as e:
        logger.error(f"Index folders error: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def cache_stats(request):
    """
    Get cache statistics.
    
    Returns:
    {
        "indexed_videos": 1234,
        "cached_clips": 456,
        "cache_size_mb": 8542.5,
        "cache_size_gb": 8.34,
        "by_folder": {"Sản phẩm": 100, ...}
    }
    """
    try:
        from django.db.models import Count, Sum
        
        # Count indexed videos
        total_videos = IndexedVideo.objects.filter(is_available=True).count()
        
        # Count by folder
        by_folder = {}
        for folder_type in set(FOLDER_TYPES):
            count = IndexedVideo.objects.filter(
                folder_type=folder_type,
                is_available=True
            ).count()
            by_folder[folder_type] = count
        
        # Cache stats
        cached_clips = VideoClipCache.objects.count()
        cache_size = VideoClipCache.objects.aggregate(
            total=Sum('file_size')
        )['total'] or 0
        
        cache_size_mb = cache_size / (1024 * 1024)
        cache_size_gb = cache_size / (1024 * 1024 * 1024)
        
        return Response({
            'indexed_videos': total_videos,
            'cached_clips': cached_clips,
            'cache_size_mb': round(cache_size_mb, 2),
            'cache_size_gb': round(cache_size_gb, 2),
            'by_folder': by_folder,
            'gpu_available': get_preprocessing_service().has_gpu()
        })
    
    except Exception as e:
        logger.error(f"Cache stats error: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def get_voices(request):
    """Get available voices for TTS."""
    try:
        from video_management.models import Voice
        
        # Get all voices (Voice model doesn't have is_active field)
        voices = Voice.objects.all().values(
            'id', 'name', 'voice_id', 'provider', 'language', 'gender', 'is_cloned', 'is_system'
        ).order_by('-is_system', 'name')  # System voices first
        
        return Response({
            'success': True,
            'voices': list(voices),
            'count': len(voices)
        }, status=status.HTTP_200_OK)
    
    except Exception as e:
        logger.error(f"Get voices error: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
def generate_audio_from_script(request):
    """Generate audio from script using selected voice."""
    try:
        script = request.data.get('script')
        voice_id = request.data.get('voice_id')
        
        logger.info(f"🎤 Generate Audio - Script: {len(script) if script else 0} chars, Voice: {voice_id}")
        
        if not script or not voice_id:
            logger.warning(f"❌ Missing data - Script: {bool(script)}, Voice: {bool(voice_id)}")
            return Response(
                {'error': 'script and voice_id are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from video_management.models import Voice
        voice = Voice.objects.filter(voice_id=voice_id).first()
        
        if not voice:
            return Response(
                {'error': f'Voice not found: {voice_id}'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Generate audio using HeyGen or other provider
        if voice.provider == 'heygen':
            # Call HeyGen TTS API
            import requests
            heygen_api_key = os.getenv('HEYGEN_API_KEY')
            
            if not heygen_api_key:
                logger.error("❌ HEYGEN_API_KEY not found in environment")
                return Response(
                    {'error': 'HeyGen API key not configured'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            logger.info(f"📞 Calling HeyGen API v2 with voice: {voice.voice_id}")
            
            # Use v2 preview endpoint
            response = requests.post(
                f'https://api.heygen.com/v2/voices/{voice.voice_id}/preview',
                headers={
                    'X-Api-Key': heygen_api_key,
                    'Content-Type': 'application/json'
                },
                json={
                    'text': script,
                    'voice_id': voice.voice_id,
                    'text_type': 'text'  # Required: "text" or "ssml"
                },
                timeout=60  # Longer timeout for audio generation
            )
            
            logger.info(f"📡 HeyGen Response: Status={response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"❌ HeyGen API failed: {response.text}")
                return Response(
                    {'error': f'HeyGen API error: {response.text}'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            data = response.json()
            logger.info(f"✅ HeyGen Response Data: {data}")
            
            # v2 API returns audio_url directly or in data object
            audio_url = data.get('audio_url') or data.get('data', {}).get('audio_url')
            
            if not audio_url:
                logger.error(f"❌ No audio_url in response: {data}")
                return Response(
                    {'error': 'No audio URL returned from HeyGen', 'response': data},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            return Response({
                'success': True,
                'audio_url': audio_url,
                'voice_name': voice.name
            }, status=status.HTTP_200_OK)
        
        else:
            return Response(
                {'error': f'Provider {voice.provider} not supported yet'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    except Exception as e:
        logger.error(f"Generate audio error: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
def smart_mix(request):
    """
    Smart mix video using pre-processing approach.
    
    POST Body (multipart or JSON):
    {
        "audio": <file>,  // or "audio_path": "path/to/audio.mp3"
        "num_outputs": 5,
        "width": 540,
        "height": 960,
        "use_gpu": true  // Optional: force GPU on/off (null = auto)
    }
    
    Returns:
    {
        "progress_id": "abc123...",
        "message": "Mix started"
    }
    """
    try:
        # Handle audio
        audio_path = None
        temp_audio = None
        
        if 'audio' in request.FILES:
            audio_file = request.FILES['audio']
            temp_audio = tempfile.NamedTemporaryFile(
                delete=False,
                suffix=Path(audio_file.name).suffix or '.mp3'
            )
            for chunk in audio_file.chunks():
                temp_audio.write(chunk)
            temp_audio.close()
            audio_path = temp_audio.name
        elif request.POST.get('audio_path'):
            audio_path = request.POST.get('audio_path')
        elif request.data.get('audio_path'):
            audio_path = request.data.get('audio_path')
        else:
            return Response(
                {'error': 'audio file or audio_path required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not os.path.isfile(audio_path):
            return Response(
                {'error': f'Audio file not found: {audio_path}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get parameters
        num_outputs = int(request.POST.get('num_outputs', 5) or 5)
        width = int(request.POST.get('width', 540) or 540)
        height = int(request.POST.get('height', 960) or 960)
        use_gpu_str = request.POST.get('use_gpu', 'auto')
        use_a4_formula_str = request.POST.get('use_a4_formula', 'false')
        
        use_gpu = None  # Auto
        if use_gpu_str == 'true':
            use_gpu = True
        elif use_gpu_str == 'false':
            use_gpu = False
        
        use_a4_formula = use_a4_formula_str.lower() == 'true'
        
        # Create progress tracking
        progress_id = uuid.uuid4().hex
        with _mix_progress_lock:
            _mix_progress[progress_id] = {
                "status": "processing",
                "percent": 0,
                "message": "Initializing...",
                "num_outputs": num_outputs,
                "error": None,
                "output_urls": None,
                "output_filenames": None,
            }
        
        # Start mix task in background
        # Pass audio_path (string) and whether it's temporary (needs cleanup)
        is_temp_audio = temp_audio is not None
        threading.Thread(
            target=_run_smart_mix_task,
            args=(progress_id, audio_path, num_outputs, width, height, use_gpu, is_temp_audio, use_a4_formula),
            daemon=True
        ).start()
        
        return Response({
            "progress_id": progress_id,
            "message": "Mix started"
        }, status=status.HTTP_202_ACCEPTED)
    
    except Exception as e:
        logger.error(f"Smart mix error: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def smart_mix_status(request, progress_id: str):
    """Get mix progress status (same format as old mix)."""
    with _mix_progress_lock:
        if progress_id not in _mix_progress:
            return Response(
                {'error': 'Progress ID not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        return Response(_mix_progress[progress_id])


def _run_smart_mix_task(
    progress_id: str,
    audio_path: str,
    num_outputs: int,
    width: int,
    height: int,
    use_gpu: Optional[bool],
    is_temp_audio: bool,
    use_a4_formula: bool = False
):
    """Background task for smart mix."""
    service = get_preprocessing_service()
    output_dir = os.path.join(settings.MEDIA_ROOT, 'mix_outputs', progress_id)
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Update progress
        _update_progress(progress_id, 5, "Preparing...")
        
        # Check if we have enough indexed videos for the selected mode
        if use_a4_formula:
            # Quick validation for A4
            from video_management.models import IndexedVideo
            available_folders = IndexedVideo.objects.filter(is_available=True).values('folder_type').distinct().count()
            if available_folders < 5:
                raise ValueError(f"Not enough folders indexed for A4. Only {available_folders} folders available. Need at least 5!")
        else:
            # Quick validation for random mode
            video_dict = service.get_random_videos(FOLDER_TYPES)
            available_count = sum(1 for v in video_dict.values() if v is not None)
            if available_count < 5:
                raise ValueError(f"Not enough videos indexed. Only {available_count} folder(s) have videos. Need at least 5!")
        
        # Generate outputs - SELECT DIFFERENT VIDEOS FOR EACH OUTPUT!
        output_files = []
        
        for i in range(num_outputs):
            _update_progress(progress_id, 10 + (i * 80 // num_outputs), f"Generating video {i+1}/{num_outputs}...")
            
            # Get FRESH video selections for each output (ensures variety!)
            if use_a4_formula:
                video_selections = _get_a4_formula_videos(service)
                logger.info(f"Output {i+1}: A4 formula with {len([v for v in video_selections if v])} videos")
            else:
                video_dict = service.get_random_videos(FOLDER_TYPES)
                video_selections = list(video_dict.values())
                logger.info(f"Output {i+1}: Random mode with {len([v for v in video_selections if v])} videos")
            
            output_file = _generate_one_mix(
                progress_id,
                i,
                video_selections,
                audio_path,
                width,
                height,
                use_gpu,
                service,
                output_dir,
                use_a4_formula
            )
            
            if output_file:
                output_files.append(output_file)
        
        # Generate URLs - Use full URL with AI service host
        ai_service_url = os.getenv('AI_SERVICE_URL', 'http://localhost:8001')
        output_urls = [
            f"{ai_service_url}{settings.MEDIA_URL}mix_outputs/{progress_id}/{os.path.basename(f)}"
            for f in output_files
        ]
        
        output_filenames = [os.path.basename(f) for f in output_files]
        
        # Complete
        with _mix_progress_lock:
            _mix_progress[progress_id].update({
                "status": "completed",
                "percent": 100,
                "message": f"Generated {len(output_files)} videos",
                "output_urls": output_urls,
                "output_filenames": output_filenames,
            })
        
        logger.info(f"✅ Smart mix completed: {progress_id} ({len(output_files)} videos)")
    
    except Exception as e:
        logger.error(f"Smart mix task error: {e}", exc_info=True)
        with _mix_progress_lock:
            _mix_progress[progress_id].update({
                "status": "error",
                "error": str(e),
                "message": f"Error: {str(e)}"
            })
    
    finally:
        # Cleanup temp audio if it was uploaded (not a permanent file)
        if is_temp_audio and audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
                logger.info(f"Cleaned up temp audio: {audio_path}")
            except Exception as e:
                logger.warning(f"Failed to cleanup temp audio {audio_path}: {e}")


def _generate_one_mix(
    progress_id: str,
    output_index: int,
    video_selections: List[Optional[int]],
    audio_path: str,
    width: int,
    height: int,
    use_gpu: Optional[bool],
    service,
    output_dir: str,
    use_a4_formula: bool = False
) -> Optional[str]:
    """Generate one mixed video using cached clips."""
    
    # Get audio duration first
    audio_duration = _get_media_duration(audio_path)
    if not audio_duration:
        logger.error(f"Failed to get audio duration: {audio_path}")
        audio_duration = 60  # Default fallback
    
    logger.info(f"Audio duration: {audio_duration}s")
    
    # Get/generate clips for each slot
    clip_paths = []
    clip_durations = []
    
    for i, video_id in enumerate(video_selections):
        if video_id is None:
            slot_name = A4_FORMULA[i]['folder_type'] if use_a4_formula and i < len(A4_FORMULA) else f"Slot {i}"
            logger.warning(f"{slot_name}: No video available, skipping")
            continue
        
        # Get duration for A4 mode
        clip_duration = A4_FORMULA[i]['duration'] if use_a4_formula and i < len(A4_FORMULA) else 8
        
        clip_path = service.get_or_generate_clip(video_id, use_gpu, duration=clip_duration)
        if clip_path and os.path.isfile(clip_path):
            clip_paths.append(clip_path)
            clip_durations.append(clip_duration)
        else:
            slot_name = A4_FORMULA[i]['folder_type'] if use_a4_formula and i < len(A4_FORMULA) else f"Slot {i}"
            logger.warning(f"{slot_name}: Failed to get clip for video {video_id}")
    
    if len(clip_paths) < 5:
        raise ValueError(f"Not enough clips generated: {len(clip_paths)} clips. Need at least 5 clips to mix.")
    
    # Calculate total video duration
    total_video_duration = sum(clip_durations)
    logger.info(f"Total video duration (1 loop): {total_video_duration}s")
    
    # Loop clips if video is shorter than audio
    if total_video_duration < audio_duration:
        loops_needed = int(audio_duration / total_video_duration) + 1
        logger.info(f"🔄 Video too short! Looping {loops_needed}x to match audio ({audio_duration}s)")
        
        original_clips = clip_paths.copy()
        original_durations = clip_durations.copy()
        
        for _ in range(loops_needed - 1):
            clip_paths.extend(original_clips)
            clip_durations.extend(original_durations)
        
        logger.info(f"After looping: {len(clip_paths)} clips, total {sum(clip_durations)}s")
    
    # Concat clips (FAST - copy codec!)
    temp_video = os.path.join(output_dir, f"temp_{output_index}.mp4")
    concat_success = _concat_clips_fast(clip_paths, temp_video)
    
    if not concat_success:
        return None
    
    # Replace audio (minimal encoding)
    output_file = os.path.join(output_dir, f"output_{output_index}.mp4")
    audio_success = _replace_audio(temp_video, audio_path, output_file)
    
    # Cleanup temp
    if os.path.exists(temp_video):
        os.remove(temp_video)
    
    if audio_success:
        return output_file
    return None


def _concat_clips_fast(clip_paths: List[str], output_path: str) -> bool:
    """Concat clips using copy codec (FAST!)."""
    try:
        # Create concat list file
        list_file = output_path + ".list.txt"
        with open(list_file, 'w', encoding='utf-8') as f:
            for clip_path in clip_paths:
                safe_path = os.path.abspath(clip_path).replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")
        
        # FFmpeg concat with copy
        ffmpeg_path = os.getenv('FFMPEG_PATH', 'ffmpeg')
        cmd = [
            ffmpeg_path,
            '-f', 'concat',
            '-safe', '0',
            '-i', list_file,
            '-c', 'copy',  # COPY codec - super fast!
            '-y',
            output_path
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',  # Ignore Unicode decode errors
            timeout=60
        )
        
        # Cleanup list file
        if os.path.exists(list_file):
            os.remove(list_file)
        
        if result.returncode != 0:
            logger.error(f"Concat failed: {result.stderr}")
        
        return result.returncode == 0 and os.path.isfile(output_path)
    
    except Exception as e:
        logger.error(f"Concat failed: {e}")
        return False


def _replace_audio(video_path: str, audio_path: str, output_path: str) -> bool:
    """Replace audio in video (minimal encoding)."""
    try:
        ffmpeg_path = os.getenv('FFMPEG_PATH', 'ffmpeg')
        
        cmd = [
            ffmpeg_path,
            '-i', video_path,
            '-i', audio_path,
            '-c:v', 'copy',  # Copy video - no re-encoding!
            '-c:a', 'aac',   # Only encode audio
            '-map', '0:v:0',
            '-map', '1:a:0',
            '-shortest',
            '-y',
            output_path
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',  # Ignore Unicode decode errors
            timeout=60
        )
        
        if result.returncode != 0:
            logger.error(f"Audio replace failed: {result.stderr}")
        
        return result.returncode == 0 and os.path.isfile(output_path)
    
    except Exception as e:
        logger.error(f"Audio replace failed: {e}")
        return False


def _update_progress(progress_id: str, percent: int, message: str):
    """Update progress tracking."""
    with _mix_progress_lock:
        if progress_id in _mix_progress:
            _mix_progress[progress_id].update({
                "percent": percent,
                "message": message
            })


def _get_media_duration(file_path: str) -> Optional[float]:
    """Get duration of audio/video file in seconds using ffprobe."""
    try:
        ffprobe_path = os.getenv('FFPROBE_PATH', 'ffprobe')
        
        cmd = [
            ffprobe_path,
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=10
        )
        
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
        
        return None
    
    except Exception as e:
        logger.error(f"Failed to get duration for {file_path}: {e}")
        return None


def _get_a4_formula_videos(service):
    """Get videos for A4 formula (structured selection)."""
    from video_management.models import IndexedVideo
    import random
    
    selected_videos = []
    
    for slot in A4_FORMULA:
        folder_type = slot['folder_type']
        
        # Get random video from this folder type
        videos = IndexedVideo.objects.filter(
            folder_type=folder_type,
            is_available=True,
            duration__gte=slot['duration']  # Must be long enough
        ).values_list('id', flat=True)
        
        if videos:
            video_id = random.choice(list(videos))
            selected_videos.append(video_id)
            logger.info(f"A4 Slot: {folder_type} ({slot['duration']}s) → Video {video_id}")
        else:
            logger.warning(f"A4 Slot: {folder_type} → No video available!")
            selected_videos.append(None)
    
    return selected_videos
