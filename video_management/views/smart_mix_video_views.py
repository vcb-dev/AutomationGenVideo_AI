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
# Default folder types for A4 Formula (7 REQUIRED folder types for 8 slots)
# Note: "HuyK" is used twice (slot 2 & 4), "SPLIT_LAYOUT" uses 2 folders (Chế tác Above 2 + HuyK)
FOLDER_TYPES = [
    "Sản phẩm",         # Slot 1: Intro
    "HuyK",             # Slot 2 + SPLIT 2 (bottom) + SPLIT 3 (top)
    "Chế tác Above 1",  # SPLIT 1 (top, 30%)
    "Chế tác Below 1",  # SPLIT 1 (bottom, 70%)
    "Chế tác Above 2",  # SPLIT 2 (top, 30%)
    "Chế tác Below 2",  # SPLIT 3 (bottom, 70%)
    "Sản phẩm HT",      # Slot 6: Sản phẩm hoàn thiện
    "Outtrol",          # Slot 7: Outro (original audio)
]

# ============================================================================
# CÔNG THỨC A4 V2 - FLEXIBLE DURATION (7 slots)
# ============================================================================
# Reference: gen-n-CongThucA4.jpg (Updated requirements)
# 
# Cấu trúc timeline mới:
# 
#   ┌─────────┬─────┬──────────┬──────────┬──────────┬──────────┬─────────┐
#   │ Sản phẩm│ HuyK│ SPLIT 1  │ SPLIT 2  │ SPLIT 3  │Sản phẩm  │ Outro   │
#   │  (Intro)│     │(CT 30/70)│(CT30/HK70)│(HK30/CT70)│   HT     │(Audio ✓)│
#   └─────────┴─────┴──────────┴──────────┴──────────┴──────────┴─────────┘
#    ◄─────────────── FLEXIBLE (audio_duration / 6) ──────────────►│ORIGINAL│
#
# FLEXIBLE DURATION (Slot 1-6):
# - duration = audio_duration / 6
# - Ví dụ: audio 45s → mỗi slot 7.5s
#
# OUTRO (Slot 7):
# - duration = video_outro.original_duration (giữ nguyên)
# - audio = video_outro.original_audio (KHÔNG replace)
#
# OUTPUT VIDEO:
# - Total = (audio_duration) + (outro_duration)
# - Ví dụ: 45s content + 5s outro = 50s total
#
# Chi tiết từng slot:
# 1. Sản phẩm (flexible)       - Intro sản phẩm
# 2. HuyK (flexible)           - Video người tạo/KOC
# 3. SPLIT 1 (flexible)        - Chế tác Above (30%) / Chế tác Below (70%)
# 4. SPLIT 2 (flexible)        - Chế tác (30%) / HuyK (70%)
# 5. SPLIT 3 (flexible)        - HuyK (30%) / Chế tác (70%)
# 6. Sản phẩm HT (flexible)    - Sản phẩm hoàn thiện
# 7. Outro (original)          - Outro HuyK/Brand (giữ nguyên audio+duration)
#
# ⚠️ LƯU Ý QUAN TRỌNG:
# - 3 SPLIT LAYOUTS sử dụng FFmpeg filter_complex với vstack
# - Mỗi split có tỉ lệ khác nhau (30/70 hoặc 70/30)
# - Mỗi lần generate 5 videos sẽ chọn ngẫu nhiên videos khác nhau
# - Slot 1-6 dùng audio nội dung, Slot 7 giữ nguyên audio gốc
# ============================================================================

A4_FORMULA = [
    {"folder_type": "Sản phẩm", "flexible": True},      # Slot 1: Intro
    {"folder_type": "HuyK", "flexible": True},          # Slot 2: HuyK
    {   # Slot 3: SPLIT 1 - Chế tác Above (30%) / Chế tác Below (70%)
        "folder_type": "SPLIT_LAYOUT",
        "flexible": True,
        "layout_name": "SPLIT_1",
        "top_video": {"folder_type": "Chế tác Above 1", "ratio": 0.3},   # 30% top
        "bottom_video": {"folder_type": "Chế tác Below 1", "ratio": 0.7}  # 70% bottom
    },
    {   # Slot 4: SPLIT 2 - Chế tác (30%) / HuyK (70%)
        "folder_type": "SPLIT_LAYOUT",
        "flexible": True,
        "layout_name": "SPLIT_2",
        "top_video": {"folder_type": "Chế tác Above 2", "ratio": 0.3},   # 30% top
        "bottom_video": {"folder_type": "HuyK", "ratio": 0.7}  # 70% bottom
    },
    {   # Slot 5: SPLIT 3 - HuyK (30%) / Chế tác (70%)
        "folder_type": "SPLIT_LAYOUT",
        "flexible": True,
        "layout_name": "SPLIT_3",
        "top_video": {"folder_type": "HuyK", "ratio": 0.3},              # 30% top
        "bottom_video": {"folder_type": "Chế tác Below 2", "ratio": 0.7}  # 70% bottom
    },
    {"folder_type": "Sản phẩm HT", "flexible": True},   # Slot 6: Sản phẩm hoàn thiện
    {"folder_type": "Outtrol", "flexible": False, "use_original_audio": True},  # Slot 7: Outro (keep audio)
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
        
        # Only HeyGen voices (Minimax removed)
        voices = Voice.objects.filter(provider='heygen').values(
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
        
        # Generate audio using HeyGen only
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
            logger.info(f"📝 Script length: {len(script)} chars (~{len(script.split())} words)")
            
            # Use v2 preview endpoint (may take long for long scripts!)
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
                timeout=300  # 5 minutes for long scripts (3000+ chars)
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
                'voice_name': voice.name,
                'provider': 'heygen'
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
        "use_gpu": true,  // Optional: force GPU on/off (null = auto)
        "use_a4_formula": true  // Optional: Use A4 formula (8 slots with split layout)
    }
    
    Returns:
    {
        "progress_id": "abc123...",
        "message": "Mix started"
    }
    """
    try:
        # Log all request parameters for debugging
        logger.info(f"📥 Smart Mix Request - POST params: {dict(request.POST)}")
        logger.info(f"📥 FILES: {list(request.FILES.keys())}")
        
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
        
        # Get audio duration ONCE (all outputs will have same duration!)
        audio_duration = _get_media_duration(audio_path)
        if not audio_duration:
            logger.error(f"Failed to get audio duration: {audio_path}")
            audio_duration = 60  # Default fallback
        
        logger.info(f"🎵 Audio duration: {audio_duration}s (all outputs will match this)")
        
        # Calculate flexible slot duration for A4 (slot 1-6 share audio duration)
        slot_duration = audio_duration / 6 if use_a4_formula else None
        
        # Check if we have enough indexed videos for the selected mode
        if use_a4_formula:
            # STRICT VALIDATION FOR A4 V2: ALL 7 SLOTS MUST HAVE VIDEOS!
            from video_management.models import IndexedVideo
            
            logger.info("🔍 Validating A4 Formula V2 requirements (7 slots, flexible duration)...")
            logger.info(f"🎵 Audio: {audio_duration}s → Slot 1-6: {slot_duration:.2f}s each, Slot 7: original")
            logger.info("⚠️ Note: Slots 3-5 use SPLIT LAYOUTS (30/70 or 70/30)")
            
            missing_slots = []
            
            for i, slot in enumerate(A4_FORMULA, start=1):
                folder_type = slot.get('folder_type')
                is_flexible = slot.get('flexible', False)
                required_duration = slot_duration if is_flexible else slot.get('duration', 3)
                
                # SPECIAL: SPLIT_LAYOUT needs to check 1 top + 1 bottom
                if folder_type == "SPLIT_LAYOUT":
                    layout_name = slot.get('layout_name', f'SPLIT_{i}')
                    logger.info(f"🔍 Slot {i}/7: {layout_name} - Checking top + bottom ({required_duration:.2f}s)...")
                    
                    # Check top video
                    top_folder = slot['top_video']['folder_type']
                    
                    count = IndexedVideo.objects.filter(
                        folder_type=top_folder,
                        is_available=True,
                        duration__gte=required_duration
                    ).count()
                    
                    if count == 0:
                        missing_slots.append(f"Slot {i} Top: {top_folder} ({required_duration:.1f}s)")
                        logger.error(f"❌ Slot {i} Top: {top_folder} - NO VIDEOS!")
                    else:
                        logger.info(f"✅ Slot {i} Top: {top_folder} - {count} videos")
                    
                    # Check bottom video
                    bottom_folder = slot['bottom_video']['folder_type']
                    
                    count = IndexedVideo.objects.filter(
                        folder_type=bottom_folder,
                        is_available=True,
                        duration__gte=required_duration
                    ).count()
                    
                    if count == 0:
                        missing_slots.append(f"Slot {i} Bottom: {bottom_folder} ({required_duration:.1f}s)")
                        logger.error(f"❌ Slot {i} Bottom: {bottom_folder} - NO VIDEOS!")
                    else:
                        logger.info(f"✅ Slot {i} Bottom: {bottom_folder} - {count} videos")
                else:
                    # NORMAL SLOT (including Outro)
                    count = IndexedVideo.objects.filter(
                        folder_type=folder_type,
                        is_available=True,
                        duration__gte=required_duration
                    ).count()
                    
                    if count == 0:
                        missing_slots.append(f"Slot {i}: {folder_type} ({required_duration:.1f}s)")
                        logger.error(f"❌ Slot {i}/7: {folder_type} - NO VIDEOS FOUND!")
                    else:
                        logger.info(f"✅ Slot {i}/7: {folder_type} ({required_duration:.1f}s) - {count} videos")
            
            if missing_slots:
                error_details = "\n".join(missing_slots)
                logger.error(f"\n⚠️ A4 V2 VALIDATION FAILED!\nMissing slots:\n{error_details}")
                logger.error("\n💡 Solution: Go to 'Quản lý Folders' and index ALL 7 folder types!")
                
                raise ValueError(
                    f"❌ A4 Formula V2 requires ALL 7 slots!\n\n"
                    f"Missing slots:\n{error_details}\n\n"
                    f"⚠️ ESPECIALLY CHECK:\n"
                    f"- Slot 7: 'Outtrol' (Outro with original audio)\n\n"
                    f"Please index all 7 folder types."
                )
            
            logger.info(f"✅ A4 V2 Validation passed! All 7 slots have videos.")
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
                video_selections = _get_a4_formula_videos(service, slot_duration)
                logger.info(f"Output {i+1}: A4 V2 formula (7 slots, {slot_duration:.2f}s each)")
            else:
                video_dict = service.get_random_videos(FOLDER_TYPES)
                video_selections = list(video_dict.values())
                logger.info(f"Output {i+1}: Random mode with {len([v for v in video_selections if v])} videos")
            
            output_file = _generate_one_mix(
                progress_id,
                i,
                video_selections,
                audio_path,
                audio_duration,  # Pass SAME duration for all outputs
                width,
                height,
                use_gpu,
                service,
                output_dir,
                use_a4_formula,
                slot_duration  # Pass flexible slot duration
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
    audio_duration: float,  # NOW PASSED FROM PARENT (same for all outputs!)
    width: int,
    height: int,
    use_gpu: Optional[bool],
    service,
    output_dir: str,
    use_a4_formula: bool = False,
    slot_duration: Optional[float] = None  # For A4 V2 flexible slots
) -> Optional[str]:
    """
    Generate one mixed video using cached clips.
    
    For A4 V2:
    - Slot 1-6: Use slot_duration (flexible, based on audio)
    - Slot 7 (Outro): Use original duration + original audio
    """
    
    logger.info(f"Output {output_index}: Target duration = {audio_duration}s" + 
                (f", Slot duration = {slot_duration:.2f}s" if slot_duration else ""))
    
    # Get/generate clips for each slot
    clip_paths = []
    clip_durations = []
    outro_clip_path = None  # For A4 V2: Outro with original audio
    outro_duration = None
    
    for i, video_selection in enumerate(video_selections):
        # Check if this is Outro slot (last slot in A4 V2)
        is_outro = (use_a4_formula and i == len(video_selections) - 1 and 
                    i < len(A4_FORMULA) and A4_FORMULA[i].get('use_original_audio'))
        
        # Check if this is a SPLIT_LAYOUT slot (dict) or normal slot (int)
        if isinstance(video_selection, dict) and video_selection.get("type") == "SPLIT_LAYOUT":
            # SPECIAL HANDLING: SPLIT LAYOUT
            clip_duration = video_selection['duration']
            layout_name = video_selection.get('layout_name', 'SPLIT')
            top_ratio = video_selection.get('top_ratio', 0.3)
            bottom_ratio = video_selection.get('bottom_ratio', 0.7)
            
            # Create split layout video
            split_output = os.path.join(output_dir, f"split_{output_index}_{i}.mp4")
            
            success = _create_split_layout_video(
                top_video_id=video_selection['top_video'],
                bottom_video_id=video_selection['bottom_video'],
                duration=clip_duration,
                output_path=split_output,
                service=service,
                use_gpu=use_gpu,
                top_ratio=top_ratio,
                bottom_ratio=bottom_ratio,
                output_dir=output_dir,
                output_index=output_index
            )
            
            if success and os.path.isfile(split_output):
                clip_paths.append(split_output)
                clip_durations.append(clip_duration)
                logger.info(f"✅ Slot {i+1}/{len(video_selections)}: {layout_name} ({int(top_ratio*100)}/{int(bottom_ratio*100)}, {clip_duration:.2f}s)")
            else:
                if use_a4_formula:
                    logger.error(f"❌ Split layout failed: success={success}, file_exists={os.path.isfile(split_output) if split_output else False}")
                    raise ValueError(f"❌ A4 Formula: Failed to create SPLIT LAYOUT for Slot {i+1}")
                logger.error(f"Failed to create SPLIT LAYOUT for slot {i+1}")
                continue
        else:
            # NORMAL SLOT
            video_id = video_selection
            
            if video_id is None:
                slot_name = A4_FORMULA[i].get('folder_type', f"Slot {i}") if use_a4_formula and i < len(A4_FORMULA) else f"Slot {i}"
                
                if use_a4_formula:
                    raise ValueError(f"❌ A4 Formula: Slot {i+1}/{len(A4_FORMULA)} ({slot_name}) has no video!")
                
                logger.warning(f"{slot_name}: No video available, skipping")
                continue
            
            # Get duration for this slot
            if use_a4_formula and i < len(A4_FORMULA):
                if is_outro:
                    # Outro: Get original video duration
                    from video_management.models import IndexedVideo
                    try:
                        indexed_video = IndexedVideo.objects.get(id=video_id)
                        clip_duration = indexed_video.duration
                        logger.info(f"🎬 Slot {i+1}: Outro with ORIGINAL duration ({clip_duration:.2f}s)")
                    except IndexedVideo.DoesNotExist:
                        clip_duration = 5  # Fallback
                        logger.warning(f"⚠️ Outro video {video_id} not found, using fallback 5s")
                elif A4_FORMULA[i].get('flexible'):
                    clip_duration = slot_duration
                else:
                    clip_duration = A4_FORMULA[i].get('duration', 8)
            else:
                clip_duration = 8
            
            # ===== USE ANTI-FREEZE HELPER =====
            if is_outro:
                # Outro: Generate normally (keep original duration)
                clip_path = service.get_or_generate_clip(video_id, use_gpu, duration=clip_duration)
            else:
                # Use auto-fill helper to prevent freeze
                fill_output = os.path.join(output_dir, f"filled_{output_index}_{i}.mp4")
                clip_path = _get_clip_with_autofill(video_id, clip_duration, use_gpu, service, fill_output)
            
            if clip_path and os.path.isfile(clip_path):
                if is_outro:
                    # Store outro separately (to preserve its original audio)
                    outro_clip_path = clip_path
                    outro_duration = clip_duration
                    logger.info(f"✅ Slot {i+1}/{len(video_selections)}: Outro (ORIGINAL AUDIO, {clip_duration:.2f}s)")
                else:
                    clip_paths.append(clip_path)
                    clip_durations.append(clip_duration)
                    
                    slot_name = A4_FORMULA[i].get('folder_type', f"Slot {i}") if use_a4_formula and i < len(A4_FORMULA) else f"Slot {i}"
                    logger.info(f"✅ Slot {i+1}/{len(video_selections)}: {slot_name} ({clip_duration:.2f}s)")
            else:
                slot_name = A4_FORMULA[i].get('folder_type', f"Slot {i}") if use_a4_formula and i < len(A4_FORMULA) else f"Slot {i}"
                
                if use_a4_formula:
                    raise ValueError(f"❌ A4 Formula: Failed to generate clip for Slot {i+1} ({slot_name})")
                
                logger.warning(f"{slot_name}: Failed to get clip for video {video_id}")
    
    # Log all clips for debugging
    logger.info(f"📋 Clip summary: {len(clip_paths)} content clips + {1 if outro_clip_path else 0} outro")
    for idx, (clip, dur) in enumerate(zip(clip_paths, clip_durations), start=1):
        is_split = "split_" in os.path.basename(clip)
        logger.info(f"  Clip {idx}: {os.path.basename(clip)} ({dur:.2f}s) {'[SPLIT LAYOUT]' if is_split else ''}")
    if outro_clip_path:
        logger.info(f"  Outro: {os.path.basename(outro_clip_path)} ({outro_duration:.2f}s) [ORIGINAL AUDIO]")
    
    # Validation
    if use_a4_formula:
        expected_content_clips = 6  # Slot 1-6
        if len(clip_paths) != expected_content_clips:
            raise ValueError(f"❌ A4 V2 Formula requires {expected_content_clips} content clips! Got {len(clip_paths)} clips.")
        if not outro_clip_path:
            raise ValueError(f"❌ A4 V2 Formula: Missing Outro (Slot 7)!")
    elif len(clip_paths) < 5:
        raise ValueError(f"Not enough clips generated: {len(clip_paths)} clips. Need at least 5 clips to mix.")
    
    # Calculate total content duration (Slot 1-6)
    total_content_duration = sum(clip_durations)
    logger.info(f"📊 Content duration (Slot 1-6): {total_content_duration:.2f}s")
    
    # For A4 V2: Content should match audio_duration (Slot 1-6 only)
    # No need to loop - slot_duration is calculated to fill audio_duration exactly
    if use_a4_formula and outro_clip_path:
        logger.info(f"🎵 A4 V2 Mode:")
        logger.info(f"  - Content (Slot 1-6): {total_content_duration:.2f}s (should match audio)")
        logger.info(f"  - Outro (Slot 7): {outro_duration:.2f}s (original audio)")
        logger.info(f"  - Total output: {total_content_duration + outro_duration:.2f}s")
    else:
        # For random mode: Loop clips if video is shorter than audio
        if total_content_duration < audio_duration:
            loops_needed = int(audio_duration / total_content_duration) + 1
            logger.info(f"🔄 Video too short! Looping {loops_needed}x to match audio ({audio_duration}s)")
            
            original_clips = clip_paths.copy()
            original_durations = clip_durations.copy()
            
            for _ in range(loops_needed - 1):
                clip_paths.extend(original_clips)
                clip_durations.extend(original_durations)
            
            logger.info(f"After looping: {len(clip_paths)} clips, total {sum(clip_durations):.2f}s")
    
    # Concat content clips (Slot 1-6) - FAST - copy codec!
    temp_content = os.path.join(output_dir, f"temp_content_{output_index}.mp4")
    concat_success = _concat_clips_fast(clip_paths, temp_content)
    
    if not concat_success:
        return None
    
    # Replace audio for content part
    content_with_audio = os.path.join(output_dir, f"content_audio_{output_index}.mp4")
    audio_success = _replace_audio(temp_content, audio_path, content_with_audio, audio_duration)
    
    # Cleanup temp content
    if os.path.exists(temp_content):
        os.remove(temp_content)
    
    if not audio_success:
        return None
    
    # Final output
    output_file = os.path.join(output_dir, f"output_{output_index}.mp4")
    
    # If A4 V2: Concat content + outro (outro keeps original audio)
    if use_a4_formula and outro_clip_path:
        logger.info("🔗 Concatenating content + outro...")
        final_concat_success = _concat_clips_fast([content_with_audio, outro_clip_path], output_file)
        
        # Cleanup temp content with audio
        if os.path.exists(content_with_audio):
            os.remove(content_with_audio)
        
        if final_concat_success:
            logger.info(f"✅ A4 V2 Final output: {os.path.basename(output_file)}")
            return output_file
        return None
    else:
        # For random mode: content_with_audio IS the final output
        if os.path.exists(content_with_audio):
            os.rename(content_with_audio, output_file)
            return output_file
        return None


def _concat_clips_fast(clip_paths: List[str], output_path: str) -> bool:
    """
    Concat clips using copy codec (FAST!).
    If copy fails (encoding mismatch), fallback to re-encoding.
    """
    if not clip_paths:
        logger.error("❌ No clips to concat!")
        return False
    
    if len(clip_paths) == 1:
        # Only 1 clip - just copy it
        try:
            import shutil
            shutil.copy2(clip_paths[0], output_path)
            logger.info(f"✅ Single clip, copied to output")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to copy single clip: {e}")
            return False
    
    try:
        # Create concat list file
        list_file = output_path + ".list.txt"
        with open(list_file, 'w', encoding='utf-8') as f:
            for clip_path in clip_paths:
                if not os.path.isfile(clip_path):
                    logger.error(f"❌ Clip không tồn tại: {clip_path}")
                    return False
                safe_path = os.path.abspath(clip_path).replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")
        
        ffmpeg_path = os.getenv('FFMPEG_PATH', 'ffmpeg')
        
        # ===== TRY 1: Fast concat with copy codec =====
        logger.info(f"🔗 Concat {len(clip_paths)} clips (TRY 1: copy codec)...")
        cmd_fast = [
            ffmpeg_path,
            '-f', 'concat',
            '-safe', '0',
            '-i', list_file,
            '-c', 'copy',  # COPY codec - super fast!
            '-y',
            output_path
        ]
        
        result = subprocess.run(
            cmd_fast,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=120
        )
        
        if result.returncode == 0 and os.path.isfile(output_path):
            logger.info(f"✅ Concat SUCCESS (copy codec) → {os.path.basename(output_path)}")
            # Cleanup
            if os.path.exists(list_file):
                os.remove(list_file)
            return True
        
        # ===== TRY 2: Fallback to re-encoding with NORMALIZATION =====
        logger.warning(f"⚠️ Copy codec failed! Stderr: {result.stderr[:200]}")
        logger.info(f"🔄 Concat {len(clip_paths)} clips (TRY 2: re-encode + normalize)...")
        
        # Remove failed output
        if os.path.exists(output_path):
            os.remove(output_path)
        
        cmd_reencode = [
            ffmpeg_path,
            '-f', 'concat',
            '-safe', '0',
            '-i', list_file,
            '-vf', 'fps=30,scale=540:960',  # ✅ NORMALIZE: Force 30fps + consistent resolution
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '28',
            '-pix_fmt', 'yuv420p',          # ✅ Consistent pixel format
            '-c:a', 'aac',
            '-ar', '44100',                 # ✅ Consistent audio sample rate
            '-ac', '2',                     # ✅ Stereo
            '-b:a', '128k',
            '-y',
            output_path
        ]
        
        result2 = subprocess.run(
            cmd_reencode,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=300  # Longer timeout for re-encoding
        )
        
        # Cleanup list file
        if os.path.exists(list_file):
            os.remove(list_file)
        
        if result2.returncode == 0 and os.path.isfile(output_path):
            logger.info(f"✅ Concat SUCCESS (re-encode) → {os.path.basename(output_path)}")
            return True
        else:
            logger.error(f"❌ Concat FAILED cả 2 lần!")
            logger.error(f"Re-encode stderr: {result2.stderr[:500]}")
            return False
    
    except Exception as e:
        logger.error(f"❌ Concat error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def _replace_audio(video_path: str, audio_path: str, output_path: str, audio_duration: float = None) -> bool:
    """
    Replace audio in video (minimal encoding).
    
    IMPORTANT: We loop video clips to match audio duration before calling this.
    If audio_duration is provided, output will be trimmed to exactly match audio length.
    """
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
        ]
        
        # Trim output to exact audio duration if specified
        if audio_duration:
            cmd.extend(['-t', str(audio_duration)])
            logger.info(f"Trimming output to audio duration: {audio_duration}s")
        
        cmd.extend(['-y', output_path])

        
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


def _get_clip_with_autofill(
    video_id: int,
    required_duration: float,
    use_gpu: Optional[bool],
    service,
    output_path: str
) -> Optional[str]:
    """
    Get video clip with auto-fill logic to prevent freeze frames.
    
    If original video is shorter than required_duration, automatically
    fetch additional videos from the same folder_type and concat them.
    
    Args:
        video_id: Primary video ID
        required_duration: Target duration needed
        use_gpu: GPU flag
        service: SmartPreprocessingService instance
        output_path: Output path for concatenated result
    
    Returns:
        Path to final clip (single or concatenated)
    """
    from video_management.models import IndexedVideo
    import random
    
    try:
        # Get original video info
        indexed_video = IndexedVideo.objects.get(id=video_id)
        actual_duration = indexed_video.duration
        folder_type = indexed_video.folder_type
        
        logger.info(f"🎬 Auto-fill: Video {video_id} ({folder_type}) - {actual_duration:.2f}s / Need: {required_duration:.2f}s")
        
        # If video is long enough, generate normally
        if actual_duration >= required_duration:
            logger.info(f"  ✅ Video đủ dài, generate {required_duration:.2f}s")
            return service.get_or_generate_clip(video_id, use_gpu, duration=required_duration)
        
        # Video too short → Auto-fill with other videos
        gap = required_duration - actual_duration
        logger.warning(f"  ⚠️ Video NGẮN! Gap: {gap:.2f}s")
        logger.info(f"  🔄 Bắt đầu auto-fill từ '{folder_type}'...")
        
        # Generate first clip (USE FULL DURATION)
        first_clip = service.get_or_generate_clip(video_id, use_gpu, duration=actual_duration)
        if not first_clip or not os.path.isfile(first_clip):
            logger.error(f"  ❌ Không generate được clip đầu!")
            return None
        
        logger.info(f"  ✅ Clip 1: {os.path.basename(first_clip)} ({actual_duration:.2f}s)")
        
        # Build clip list & track used videos
        fill_clips = [first_clip]
        total_duration = actual_duration
        used_video_ids = {video_id}  # Track để tránh duplicate
        
        # Fetch more videos until we have enough duration
        attempt = 0
        max_attempts = 15  # Prevent infinite loop
        
        while total_duration < required_duration and attempt < max_attempts:
            attempt += 1
            remaining = required_duration - total_duration
            
            logger.info(f"  🔍 Attempt {attempt}: Cần thêm {remaining:.2f}s")
            
            # Dynamic minimum duration requirement
            if remaining >= 3:
                min_duration = 3
            elif remaining >= 1:
                min_duration = 1
            else:
                min_duration = 0.5  # Accept very short clips
            
            # Query available videos (exclude already used)
            other_videos = list(IndexedVideo.objects.filter(
                folder_type=folder_type,
                is_available=True,
                duration__gte=min_duration
            ).exclude(id__in=used_video_ids).values_list('id', 'duration'))
            
            if not other_videos:
                # Try lowering requirement
                if min_duration > 0.5:
                    logger.warning(f"  ⚠️ Không tìm thấy video >= {min_duration}s, thử tìm video ngắn hơn...")
                    other_videos = list(IndexedVideo.objects.filter(
                        folder_type=folder_type,
                        is_available=True,
                        duration__gt=0
                    ).exclude(id__in=used_video_ids).values_list('id', 'duration'))
                
                if not other_videos:
                    logger.warning(f"  ⚠️ HẾT VIDEO trong '{folder_type}'!")
                    logger.warning(f"  ⚠️ Dừng tại {total_duration:.2f}s / {required_duration:.2f}s")
                    break
            
            # Pick random video
            next_video_id, next_video_duration = random.choice(other_videos)
            used_video_ids.add(next_video_id)
            
            # Use as much duration as needed (but not more than video has)
            next_duration = min(next_video_duration, remaining + 0.2)  # Small buffer
            
            logger.info(f"  📹 Video {next_video_id}: {next_video_duration:.2f}s → Sử dụng {next_duration:.2f}s")
            
            next_clip = service.get_or_generate_clip(next_video_id, use_gpu, duration=next_duration)
            
            if next_clip and os.path.isfile(next_clip):
                fill_clips.append(next_clip)
                total_duration += next_duration
                logger.info(f"  ✅ Clip {len(fill_clips)}: Added ({next_duration:.2f}s)")
                logger.info(f"  📊 Total: {total_duration:.2f}s / {required_duration:.2f}s ({len(fill_clips)} clips)")
            else:
                logger.warning(f"  ⚠️ Không generate được video {next_video_id}, thử video khác...")
                continue  # Try next video instead of breaking
        
        # Final validation
        if total_duration < required_duration:
            gap = required_duration - total_duration
            logger.warning(f"  ⚠️ CẢNH BÁO: Vẫn thiếu {gap:.2f}s!")
            logger.warning(f"  ⚠️ Video có thể bị đứng hình {gap:.2f}s cuối!")
        
        # If only 1 clip, return it directly
        if len(fill_clips) == 1:
            logger.info(f"  ➡️ Chỉ 1 clip, return trực tiếp")
            return first_clip
        
        # Concat multiple clips
        logger.info(f"  🔗 Concat {len(fill_clips)} clips → {total_duration:.2f}s")
        concat_success = _concat_clips_fast(fill_clips, output_path)
        
        if concat_success and os.path.isfile(output_path):
            logger.info(f"  ✅ Auto-fill SUCCESS: {os.path.basename(output_path)}")
            return output_path
        else:
            logger.error(f"  ❌ Concat FAILED! Đây là BUG nghiêm trọng!")
            logger.error(f"  ❌ Video sẽ BỊ ĐỨNG HÌNH vì chỉ dùng clip đầu ({actual_duration:.2f}s < {required_duration:.2f}s)")
            return first_clip
    
    except IndexedVideo.DoesNotExist:
        logger.error(f"❌ Video {video_id} not found in DB!")
        # Fallback: try to generate anyway
        logger.warning(f"⚠️ Fallback: Generate với duration={required_duration:.2f}s")
        return service.get_or_generate_clip(video_id, use_gpu, duration=required_duration)
    except Exception as e:
        logger.error(f"❌ Auto-fill error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        # Fallback
        logger.warning(f"⚠️ Fallback: Generate với duration={required_duration:.2f}s")
        return service.get_or_generate_clip(video_id, use_gpu, duration=required_duration)


def _create_split_layout_video(
    top_video_id: int,
    bottom_video_id: int,
    duration: float,
    output_path: str,
    service,
    use_gpu: Optional[bool],
    top_ratio: float = 0.3,
    bottom_ratio: float = 0.7,
    output_dir: str = None,
    output_index: int = 0
) -> bool:
    """
    Create SPLIT LAYOUT video with flexible ratios + ANTI-FREEZE logic.
    
    Supports multiple layouts:
    - 30/70: Top 30%, Bottom 70% (SPLIT_1, SPLIT_2)
    - 70/30: Top 70%, Bottom 30% (SPLIT_3)
    
    Total height: 960px (9:16 aspect ratio)
    Width: 540px
    
    Anti-freeze: Automatically fetches additional videos if original is too short.
    """
    if output_dir is None:
        output_dir = os.path.dirname(output_path)
    try:
        # Calculate heights based on ratios
        total_height = 960
        width = 540
        top_height = int(total_height * top_ratio)
        bottom_height = int(total_height * bottom_ratio)
        
        logger.info(f"🎬 Creating SPLIT LAYOUT ({int(top_ratio*100)}/{int(bottom_ratio*100)}): "
                   f"Top={top_video_id}, Bottom={bottom_video_id}, Duration={duration:.2f}s")
        logger.info(f"  Layout: Top={top_height}px, Bottom={bottom_height}px")
        logger.info(f"  ✨ Scale: COVER + CROP FROM CENTER (full màn hình, không viền đen)")
        logger.info(f"  🔄 Anti-freeze: Auto-fill nếu video ngắn hơn {duration:.1f}s")
        logger.info(f"  🎨 Effect: CLEAN vstack (không hiệu ứng thêm)")
        
        # ===== ANTI-FREEZE FOR SPLIT LAYOUT =====
        # Get top video clip(s) - với anti-freeze
        logger.info(f"  Generating top clip ({int(top_ratio*100)}%)...")
        top_fill_path = os.path.join(output_dir, f"split_top_filled_{output_index}.mp4")
        top_clip = _get_clip_with_autofill(
            top_video_id, 
            duration, 
            use_gpu, 
            service, 
            top_fill_path
        )
        if not top_clip:
            logger.error(f"❌ Failed to get top clip for video {top_video_id}")
            return False
        logger.info(f"  ✅ Top clip ready: {top_clip}")
        
        # Get bottom video clip(s) - với anti-freeze
        logger.info(f"  Generating bottom clip ({int(bottom_ratio*100)}%)...")
        bottom_fill_path = os.path.join(output_dir, f"split_bottom_filled_{output_index}.mp4")
        bottom_clip = _get_clip_with_autofill(
            bottom_video_id, 
            duration, 
            use_gpu, 
            service, 
            bottom_fill_path
        )
        if not bottom_clip:
            logger.error(f"❌ Failed to get bottom clip for video {bottom_video_id}")
            return False
        logger.info(f"  ✅ Bottom clip ready: {bottom_clip}")
        
        # FFmpeg filter: Vertical stack with SOFT BLUR FADE effect (mềm mại, mờ dần)
        # Strategy: 
        # 1. Scale + crop video (giữ aspect ratio)
        # 2. Stack vertically
        # 3. Add multiple gradient layers với alpha khác nhau → Tạo soft fade
        ffmpeg_path = os.getenv('FFMPEG_PATH', 'ffmpeg')
        
        cmd = [
            ffmpeg_path,
            '-i', top_clip,     # Input 0: Top video
            '-i', bottom_clip,  # Input 1: Bottom video
            '-filter_complex',
            # Top video: FPS normalize + Scale to FILL + Smart crop from center
            # ✅ fps=30: CRITICAL to prevent speed up/slow down!
            f'[0:v]fps=30,scale={width}:{top_height}:force_original_aspect_ratio=increase,'
            f'crop={width}:{top_height},setsar=1[top];'
            # Bottom video: Same strategy
            f'[1:v]fps=30,scale={width}:{bottom_height}:force_original_aspect_ratio=increase,'
            f'crop={width}:{bottom_height},setsar=1[bottom];'
            # Stack vertically (CLEAN - không thêm effect gì)
            '[top][bottom]vstack=inputs=2',
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '28',
            '-pix_fmt', 'yuv420p',  # ✅ Consistent pixel format
            '-c:a', 'aac',
            '-ar', '44100',         # ✅ Consistent audio sample rate
            '-ac', '2',             # ✅ Stereo
            '-t', str(duration),
            '-y',
            output_path
        ]
        
        logger.info(f"  🎨 Running FFmpeg to create SPLIT LAYOUT...")
        logger.info(f"  Command: {' '.join([str(c) for c in cmd[:10]])}...")  # Log first 10 args
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=120
        )
        
        if result.returncode != 0:
            logger.error(f"❌ Split layout FFmpeg failed!")
            logger.error(f"Error: {result.stderr[:500]}")
            return False
        
        if os.path.isfile(output_path):
            file_size = os.path.getsize(output_path) / (1024*1024)
            logger.info(f"  ✅ SPLIT LAYOUT created successfully: {output_path} ({file_size:.1f} MB)")
            return True
        else:
            logger.error(f"❌ Output file not created: {output_path}")
            return False
    
    except Exception as e:
        logger.error(f"Split layout error: {e}", exc_info=True)
        return False


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


def _get_a4_formula_videos(service, slot_duration):
    """
    Get videos for A4 Formula V2 (flexible duration, 7 slots).
    
    CRITICAL: ALL 7 SLOTS MUST HAVE VIDEOS!
    Slots 3-5 are SPECIAL: SPLIT LAYOUTS with different ratios
    
    Args:
        service: SmartPreprocessingService instance
        slot_duration: Duration for flexible slots (1-6)
    
    Returns:
        List of video IDs or SPLIT_LAYOUT dicts
    """
    from video_management.models import IndexedVideo
    import random
    
    selected_videos = []
    missing_slots = []
    
    for i, slot in enumerate(A4_FORMULA, start=1):
        folder_type = slot.get('folder_type')
        is_flexible = slot.get('flexible', False)
        required_duration = slot_duration if is_flexible else slot.get('duration', 3)
        
        # SPECIAL HANDLING for SPLIT_LAYOUT (Slots 3, 4, 5)
        if folder_type == "SPLIT_LAYOUT":
            layout_name = slot.get('layout_name', f'SPLIT_{i}')
            top_ratio = slot['top_video']['ratio']
            bottom_ratio = slot['bottom_video']['ratio']
            
            logger.info(f"🎬 A4 Slot {i}/7: {layout_name} ({int(top_ratio*100)}/{int(bottom_ratio*100)}, {required_duration:.2f}s)")
            
            # Select 1 video for top
            top_folder = slot['top_video']['folder_type']
            
            videos = IndexedVideo.objects.filter(
                folder_type=top_folder,
                is_available=True,
                duration__gte=required_duration
            ).values_list('id', flat=True)
            
            if videos:
                top_vid_id = random.choice(list(videos))
                logger.info(f"  ✅ Top ({int(top_ratio*100)}%): {top_folder} → Video {top_vid_id}")
            else:
                logger.error(f"  ❌ Top: {top_folder} → NO VIDEO!")
                top_vid_id = None
                missing_slots.append(f"Slot {i} Top: {top_folder}")
            
            # Select 1 video for bottom
            bottom_folder = slot['bottom_video']['folder_type']
            
            videos = IndexedVideo.objects.filter(
                folder_type=bottom_folder,
                is_available=True,
                duration__gte=required_duration
            ).values_list('id', flat=True)
            
            if videos:
                bottom_vid_id = random.choice(list(videos))
                logger.info(f"  ✅ Bottom ({int(bottom_ratio*100)}%): {bottom_folder} → Video {bottom_vid_id}")
            else:
                logger.error(f"  ❌ Bottom: {bottom_folder} → NO VIDEO!")
                bottom_vid_id = None
                missing_slots.append(f"Slot {i} Bottom: {bottom_folder}")
            
            # Store as dict for special handling
            selected_videos.append({
                "type": "SPLIT_LAYOUT",
                "layout_name": layout_name,
                "top_video": top_vid_id,
                "bottom_video": bottom_vid_id,
                "top_ratio": top_ratio,
                "bottom_ratio": bottom_ratio,
                "duration": required_duration
            })
        else:
            # Normal slot (including Outro with original audio)
            videos = IndexedVideo.objects.filter(
                folder_type=folder_type,
                is_available=True,
                duration__gte=required_duration
            ).values_list('id', flat=True)
            
            video_count = len(videos)
            
            if videos:
                video_id = random.choice(list(videos))
                selected_videos.append(video_id)
                
                # Mark Outro as special
                if slot.get('use_original_audio'):
                    logger.info(f"✅ A4 Slot {i}/7: {folder_type} (OUTRO, original audio) → Video {video_id} (Pool: {video_count})")
                else:
                    logger.info(f"✅ A4 Slot {i}/7: {folder_type} ({required_duration:.2f}s) → Video {video_id} (Pool: {video_count})")
            else:
                logger.error(f"❌ A4 Slot {i}/7: {folder_type} ({required_duration:.2f}s) → NO VIDEO FOUND!")
                selected_videos.append(None)
                missing_slots.append(f"Slot {i}: {folder_type}")
    
    # CRITICAL VALIDATION
    if missing_slots:
        error_msg = f"❌ A4 Formula V2 FAILED! Missing slots:\n" + "\n".join(missing_slots)
        logger.error(error_msg)
        raise ValueError(error_msg + "\n\nPlease index ALL required folder types!")
    
    logger.info(f"✅ A4 Formula V2 complete: Selected 7/7 slots successfully!")
    return selected_videos
