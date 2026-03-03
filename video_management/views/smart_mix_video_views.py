"""
Smart Mix Video Views - NEW High-Performance Mix Solution

This module uses Smart Pre-processing approach:
- Index videos once (fast metadata scan)
- Generate clips on-demand with caching
- Mix using concat (copy codec) for speed

Performance: 5-13 seconds per mix (vs 2-3 minutes with old approach)
"""

import os
import re
import uuid
import hashlib
import logging
import tempfile
import subprocess
import threading
import time as time_module
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
   
from django.conf import settings
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from video_management.services.smart_preprocessing_service import get_preprocessing_service
from video_management.models import IndexedVideo, VideoClipCache, Product
from video_management.views.smart_mix_video_views_helper import _auto_index_manufacturing_folders, _auto_index_by_sku_global
from video_management.views.mix_progress_store import (
    progress_set, progress_get, progress_update, progress_exists, progress_get_field
)

logger = logging.getLogger(__name__)

# ── Audio Cache Directory ────────────────────────────────────────────────────
AUDIO_CACHE_DIR = os.path.join(settings.MEDIA_ROOT, 'audio_cache')
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

# ── Concurrent Mix Limiter ───────────────────────────────────────────────────
# Giới hạn tối đa 3 job mix chạy song song.
# Nếu nhiều user cùng bấm mix → xếp hàng đợi (không crash server).
_MIX_MAX_CONCURRENT = 3
_mix_semaphore = threading.Semaphore(_MIX_MAX_CONCURRENT)

# Backward-compat: vẫn giữ lock cho các hàm nội bộ dùng 
_mix_progress_lock = threading.Lock()

# Folder types for A4 Formula V3 (7 SIMPLE SLOTS - NO SPLIT)
# Simplified structure: No split layouts, just 7 sequential slots
FOLDER_TYPES = [
    "Sản phẩm",         # Slot 1: Intro sản phẩm
    "HuyK",             # Slot 2: KOC (HuyK)
    "Chế tác",          # Slot 3: Chế tác
    # "HuyK" reused      # Slot 4: KOC (HuyK) - same folder type
    # "Chế tác" reused   # Slot 5: Chế tác - same folder type
    "Sản phẩm HT",      # Slot 6: Sản phẩm hoàn thiện
    "Outtrol",          # Slot 7: Outro (original audio)
]

# ============================================================================
# CÔNG THỨC A4 V3 - SIMPLE 7 SLOTS (NO SPLIT LAYOUTS)
# ============================================================================
# Updated: 2026-02-12 - Simplified structure per user request
# 
# Cấu trúc timeline mới (KHÔNG CÓ SPLIT):
# 
#   ┌─────────┬─────┬─────────┬─────┬─────────┬─────────┬─────────┐
#   │ Sản phẩm│ KOC │ Chế tác │ KOC │ Chế tác │Sản phẩm │ Outro   │
#   │  (Intro)│(HuyK)│         │(HuyK)│         │   HT    │(Audio ✓)│
#   └─────────┴─────┴─────────┴─────┴─────────┴─────────┴─────────┘
#    ◄─────────────── FLEXIBLE (audio_duration / 6) ──────────────►│ORIGINAL│
#
# FLEXIBLE DURATION (Slot 1-6):
# - duration = audio_duration / 6
# - Ví dụ: audio 48s → mỗi slot 8s
#
# OUTRO (Slot 7):
# - duration = video_outro.original_duration (giữ nguyên)
# - audio = video_outro.original_audio (KHÔNG replace)
#
# OUTPUT VIDEO:
# - Total = (audio_duration) + (outro_duration)
# - Ví dụ: 48s content + 5s outro = 53s total
#
# Chi tiết từng slot:
# 1. Sản phẩm (flexible)       - Intro sản phẩm
# 2. KOC/HuyK (flexible)       - Video người tạo/KOC
# 3. Chế tác (flexible)        - Video chế tác
# 4. KOC/HuyK (flexible)       - Video người tạo/KOC (lặp lại)
# 5. Chế tác (flexible)        - Video chế tác (lặp lại)
# 6. Sản phẩm HT (flexible)    - Sản phẩm hoàn thiện
# 7. Outro (original)          - Outro HuyK/Brand (giữ nguyên audio+duration)
#
# ⚠️ LƯU Ý QUAN TRỌNG:
# - KHÔNG CÒN SPLIT LAYOUTS (đã loại bỏ)
# - Tất cả 7 slots đều là video đơn giản, fullscreen
# - Slot 2 và 4 dùng cùng folder "HuyK" (chọn video khác nhau)
# - Slot 3 và 5 dùng cùng folder "Chế tác" (chọn video khác nhau)
# - Mỗi lần generate 5 videos sẽ chọn ngẫu nhiên videos khác nhau
# - Slot 1-6 dùng audio nội dung, Slot 7 giữ nguyên audio gốc
# ============================================================================

A4_FORMULA = [
    {"folder_type": "Sản phẩm", "flexible": True},      # Slot 1: Intro sản phẩm
    {"folder_type": "HuyK", "flexible": True},          # Slot 2: KOC (HuyK)
    {"folder_type": "Chế tác", "flexible": True},       # Slot 3: Chế tác
    {"folder_type": "HuyK", "flexible": True},          # Slot 4: KOC (HuyK) - reuse
    {"folder_type": "Chế tác", "flexible": True},       # Slot 5: Chế tác - reuse
    {"folder_type": "Sản phẩm HT", "flexible": True},   # Slot 6: Sản phẩm hoàn thiện
    {"folder_type": "Outtrol", "flexible": False, "use_original_audio": True},  # Slot 7: Outro (keep audio)
]


@api_view(['POST'])
def index_folders(request):
    """
    Index videos from folders into database (one-time setup).
    After indexing, automatically starts background pre-generation of clips.
    
    POST Body:
    {
        "folders": {
            "Sản phẩm": "\\\\VCB_MEDIA\\...",
            "HuyK": "\\\\VCB_MEDIA\\...",
            ...
        },
        "videos_per_folder": 100  // Max videos to index per folder (0 = unlimited)
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
        
        # ── AUTO-START BACKGROUND PRE-GENERATION ────────────────────────
        # Generate clips in background so mix is instant next time!
        from video_management.services.smart_preprocessing_service import (
            start_background_pregen, get_pregen_progress
        )
        start_background_pregen(clip_duration=12.0)
        pregen = get_pregen_progress()
        # ─────────────────────────────────────────────────────────
        
        return Response({
            'success': True,
            'results': results,
            'total_indexed': total,
            'message': f'Indexed {total} videos from {len(folders)} folders',
            'pregen_status': pregen.get('status', 'idle'),
            'pregen_message': pregen.get('message', ''),
        })
    
    except Exception as e:
        logger.error(f"Index folders error: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@api_view(['POST'])
def clear_index(request):
    """
    Xóa toàn bộ indexed videos và clip cache (reset để re-index từ đầu).
    POST /api/videos/clear-index/
    Body (optional): { "folder_types": ["Sản phẩm", "HuyK"] } để chỉ xóa một số folder_type.
    """
    try:
        folder_types = request.data.get('folder_types', None)  # None = xóa tất cả
        clear_clips = request.data.get('clear_clips', True)    # Xóa cả clip cache
        
        from django.db import transaction
        
        with transaction.atomic():
            if folder_types:
                # Chỉ xóa các folder_type chỉ định
                deleted_videos = IndexedVideo.objects.filter(folder_type__in=folder_types).count()
                IndexedVideo.objects.filter(folder_type__in=folder_types).delete()
                
                if clear_clips:
                    # Xóa clip cache liên quan (không có FK nên phải xóa hết)
                    deleted_clips = VideoClipCache.objects.count()
                    VideoClipCache.objects.all().delete()
                else:
                    deleted_clips = 0
                    
                msg = f"Xóa {deleted_videos} videos (folder: {folder_types})"
            else:
                # Xóa TẤT CẢ
                deleted_videos = IndexedVideo.objects.count()
                IndexedVideo.objects.all().delete()
                
                if clear_clips:
                    deleted_clips = VideoClipCache.objects.count()
                    VideoClipCache.objects.all().delete()
                else:
                    deleted_clips = 0
                    
                msg = f"Xóa toàn bộ {deleted_videos} indexed videos"
        
        if clear_clips:
            msg += f" + {deleted_clips} cached clips"
        
        logger.info(f"🗑️ Clear index: {msg}")
        
        return Response({
            'success': True,
            'message': msg,
            'deleted_videos': deleted_videos,
            'deleted_clips': deleted_clips if clear_clips else 0,
        })
    
    except Exception as e:
        logger.error(f"Clear index error: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def pregen_status(request):
    """
    Get background pre-generation progress.
    
    Returns:
    {
        "status": "running",  // idle | running | completed | error
        "total": 100,
        "done": 45,
        "cached": 30,
        "generated": 15,
        "failed": 0,
        "percent": 45,
        "message": "Pre-generating... 15/70 done"
    }
    """
    from video_management.services.smart_preprocessing_service import get_pregen_progress
    return Response(get_pregen_progress())



@api_view(['POST'])
def pregen_start(request):
    """
    Manually start or restart background pre-generation.
    Useful when new folders are indexed from auto-index during mix.
    """
    from video_management.services.smart_preprocessing_service import start_background_pregen
    clip_duration = float(request.data.get('clip_duration', 12.0))
    start_background_pregen(clip_duration=clip_duration)
    
    from video_management.services.smart_preprocessing_service import get_pregen_progress
    return Response({
        'success': True,
        'message': 'Pre-generation started',
        'progress': get_pregen_progress(),
    })


@api_view(['POST'])
def pregen_cancel(request):
    """Cancel running pre-generation."""
    from video_management.services.smart_preprocessing_service import cancel_pregen
    cancel_pregen()
    return Response({'success': True, 'message': 'Pre-generation cancel requested'})


@api_view(['POST'])
def index_outro(request):
    """
    Smart scan and index Outro folder.
    Automatically finds folder containing 'outro' (case-insensitive).
    
    Returns:
    {
        "success": true,
        "message": "Found and indexed Outro folder",
        "folder_path": "\\\\VCB_MEDIA\\..."
    }
    """
    try:
        service = get_preprocessing_service()
        _auto_index_outro(service)
        
        # Check if indexing succeeded
        from video_management.models import IndexedVideo
        outro_count = IndexedVideo.objects.filter(folder_type="Outtrol").count()
        
        if outro_count > 0:
            return Response({
                'success': True,
                'message': f'Successfully indexed {outro_count} Outro videos',
                'indexed_count': outro_count
            })
        else:
            return Response({
                'success': False,
                'error': 'Could not find Outro folder'
            }, status=status.HTTP_404_NOT_FOUND)
    
    except Exception as e:
        logger.error(f"Index Outro error: {e}", exc_info=True)
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


# ============================================================================
# AUDIO GENERATION - OPTIMIZED WITH CACHING + PARALLEL CHUNKING
# ============================================================================
# Performance improvements:
# 1. Cache: SHA256(script+voice_id) → instant return if cached (<100ms)
# 2. Chunking: Long scripts split at sentence boundaries → parallel API calls
# 3. Merge: ffmpeg concat for multi-chunk audio → ~2-3x faster than sequential
# ============================================================================

def _preprocess_script_for_tts(script: str) -> str:
    """
    Tiền xử lý script trước khi gửi TTS để voice tự nhiên hơn.
    
    Cải thiện:
    1. Thêm dấu phẩy sau các từ nối dài → giúp TTS nghỉ hơi đúng chỗ
    2. Ba chấm (...) → dấu chấm + khoảng nghỉ
    3. Xử lý từ viết tắt/số để TTS đọc tốt hơn
    4. Loại bỏ ký tự đặc biệt gây lỗi TTS
    """
    if not script:
        return script
    
    text = script.strip()
    
    # 1. Ba chấm → dấu chấm (TTS đọc ba chấm hay bị nuốt)
    text = re.sub(r'\.{3,}', '.', text)
    
    # 2. Thêm dấu phẩy sau các từ/cụm từ nối dài nếu CHƯA có dấu câu theo sau
    # Giúp TTS biết chỗ nghỉ hơi
    pause_words = [
        'Thật lòng mà nói', 'Để anh chị hiểu', 'Nói thật',
        'Thật ra', 'Thực ra', 'Nói chung', 'Tóm lại',
        'Ý là', 'Có nghĩa là', 'Đặc biệt', 'Quan trọng là',
        'Đầu tiên', 'Tiếp theo', 'Cuối cùng', 'Ngoài ra',
        'Tuy nhiên', 'Nhưng mà', 'Thế nhưng', 'Vì vậy',
        'Cho nên', 'Bởi vì', 'Do đó', 'Nhờ vậy',
    ]
    for pw in pause_words:
        # Thêm dấu phẩy sau cụm từ nối nếu chưa có dấu câu theo sau
        text = re.sub(
            rf'({re.escape(pw)})(\s+)(?![,\.\!\?])',
            rf'\1,\2',
            text,
            flags=re.IGNORECASE
        )
    
    # 3. Số tiền/số lượng → thêm dấu phẩy phía sau nếu thiếu
    # "100tr " → "100tr, " (giúp TTS ngắt)
    text = re.sub(r'(\d+(?:tr|k|m|tỷ|triệu|nghìn|ngàn))\s+(?=[A-ZĐÀÁẠÂa-z])', r'\1, ', text)
    
    # 4. Chất liệu / thuật ngữ: thêm dấu phẩy sau cụm liệt kê
    # "Bạc S925 lành tính" → OK (đã có context)
    
    # 5. Loại bỏ icon/emoji nếu còn sót
    text = re.sub(r'[^\w\s\.,;:!?\-\'\"\(\)…–—/]', '', text)
    
    # 6. Nhiều dấu cách → 1 dấu cách
    text = re.sub(r'\s+', ' ', text).strip()
    
    # 7. Đảm bảo có dấu chấm cuối
    if text and text[-1] not in '.!?':
        text += '.'
    
    return text


def _split_script_into_chunks(script: str, max_chars: int = 500) -> List[str]:
    """
    Split script into chunks at NATURAL sentence boundaries.
    Ưu tiên cắt tại: dấu chấm > dấu chấm phẩy > dấu phẩy.
    KHÔNG BAO GIỜ cắt giữa từ.
    """
    if len(script) <= max_chars:
        return [script]

    # Split by sentence endings (dấu chấm, chấm hỏi, chấm than)
    sentences = re.split(r'(?<=[.!?。！？])\s+', script)

    chunks = []
    current_chunk = ""

    for sentence in sentences:
        if not sentence.strip():
            continue
        
        # Nếu 1 câu đã quá dài → chia thêm tại dấu phẩy/chấm phẩy
        if len(sentence) > max_chars:
            # Chia câu dài tại dấu phẩy
            sub_parts = re.split(r'(?<=[,;])\s+', sentence)
            for part in sub_parts:
                if len(current_chunk) + len(part) > max_chars and current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = part
                else:
                    current_chunk += (" " if current_chunk else "") + part
        elif len(current_chunk) + len(sentence) > max_chars and current_chunk:
            chunks.append(current_chunk.strip())
            current_chunk = sentence
        else:
            current_chunk += (" " if current_chunk else "") + sentence

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks if chunks else [script]




def _call_heygen_tts_chunk(chunk_text: str, voice_id: str, api_key: str, chunk_idx: int) -> Dict:
    """
    Call HeyGen TTS API for a single text chunk.
    Returns dict with 'audio_url' or 'error'.
    """
    try:
        import requests as http_requests
        start = time_module.time()

        response = http_requests.post(
            f'https://api.heygen.com/v2/voices/{voice_id}/preview',
            headers={
                'X-Api-Key': api_key,
                'Content-Type': 'application/json'
            },
            json={
                'text': chunk_text,
                'voice_id': voice_id,
                'text_type': 'text'
            },
            timeout=120  # 2 minutes per chunk (shorter chunks = faster)
        )

        elapsed = time_module.time() - start

        if response.status_code != 200:
            logger.error(f"❌ Chunk {chunk_idx} failed ({elapsed:.1f}s): {response.text[:200]}")
            return {'error': f'Chunk {chunk_idx} failed: {response.text[:200]}'}

        data = response.json()
        audio_url = data.get('audio_url') or data.get('data', {}).get('audio_url')

        if not audio_url:
            return {'error': f'Chunk {chunk_idx}: No audio_url in response'}

        logger.info(f"✅ Chunk {chunk_idx}: {len(chunk_text)} chars → {elapsed:.1f}s")
        return {'audio_url': audio_url, 'chunk_idx': chunk_idx}

    except Exception as e:
        logger.error(f"❌ Chunk {chunk_idx} exception: {e}")
        return {'error': str(e)}


def _download_audio_file(url: str, output_path: str) -> bool:
    """Download audio from URL to local file."""
    try:
        import requests as http_requests
        response = http_requests.get(url, timeout=60)
        if response.status_code == 200:
            with open(output_path, 'wb') as f:
                f.write(response.content)
            return True
        logger.error(f"Download failed: status={response.status_code}")
    except Exception as e:
        logger.error(f"Download audio error: {e}")
    return False


def _get_ffmpeg_exe() -> str:
    """
    Tìm đường dẫn ffmpeg:
    1. Thử 'ffmpeg' trong PATH
    2. Fallback sang imageio_ffmpeg (bundled với project)
    """
    import shutil
    ffmpeg_in_path = shutil.which('ffmpeg')
    if ffmpeg_in_path:
        return ffmpeg_in_path
    
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    
    raise FileNotFoundError(
        "ffmpeg không tìm thấy! Hãy cài ffmpeg hoặc thêm vào PATH.\n"
        "Download: https://ffmpeg.org/download.html"
    )


def _merge_audio_files(audio_paths: List[str], output_path: str) -> bool:
    """Merge multiple audio files using ffmpeg concat demuxer."""
    try:
        ffmpeg_exe = _get_ffmpeg_exe()
        logger.info(f"🔧 Using ffmpeg: {ffmpeg_exe}")

        # Create concat list file (dùng forward slashes cho ffmpeg)
        list_path = output_path + '.concat.txt'
        with open(list_path, 'w', encoding='utf-8') as f:
            for path in audio_paths:
                safe_path = path.replace('\\', '/')
                f.write(f"file '{safe_path}'\n")

        cmd = [
            ffmpeg_exe, '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', list_path,
            '-c', 'copy',
            output_path
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60
        )

        # Cleanup concat list
        if os.path.isfile(list_path):
            os.unlink(list_path)

        if result.returncode != 0:
            logger.error(f"ffmpeg merge failed: {result.stderr[:300]}")
            return False

        return True
    except Exception as e:
        logger.error(f"Merge audio error: {e}")
        return False




@api_view(['POST'])
def generate_audio_from_script(request):
    """
    Generate audio from script using selected voice.
    
    OPTIMIZED with:
    - Audio caching (SHA256 of script+voice_id) → instant if cached
    - Parallel chunking for long scripts → 2-3x faster
    - Local file storage → reliable playback
    """
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
        
        start_time = time_module.time()
        
        from video_management.models import Voice
        voice = Voice.objects.filter(voice_id=voice_id).first()
        
        if not voice:
            return Response(
                {'error': f'Voice not found: {voice_id}'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # ── STEP 1: Check cache ──────────────────────────────────────────────
        cache_key = hashlib.sha256(f"{voice_id}:{script}".encode('utf-8')).hexdigest()
        cache_path = os.path.join(AUDIO_CACHE_DIR, f"{cache_key}.mp3")
        
        if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
            elapsed = time_module.time() - start_time
            logger.info(f"⚡ CACHE HIT! Audio returned in {elapsed:.3f}s (key={cache_key[:12]})")
            
            # Build URL to serve cached audio
            audio_serve_url = request.build_absolute_uri(f'/api/audio/cache/{cache_key}.mp3')
            
            return Response({
                'success': True,
                'audio_url': audio_serve_url,
                'voice_name': voice.name,
                'provider': 'heygen',
                'cached': True,
                'elapsed': round(elapsed, 3)
            }, status=status.HTTP_200_OK)
        
        # ── STEP 2: Generate audio ───────────────────────────────────────────
        if voice.provider != 'heygen':
            return Response(
                {'error': f'Provider {voice.provider} not supported yet'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        import requests as http_requests
        heygen_api_key = os.getenv('HEYGEN_API_KEY')
        
        if not heygen_api_key:
            logger.error("❌ HEYGEN_API_KEY not found in environment")
            return Response(
                {'error': 'HeyGen API key not configured'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        # ── STEP 2.5: Preprocess script cho TTS tự nhiên ────────────────────
        script = _preprocess_script_for_tts(script)
        logger.info(f"📝 After TTS preprocessing: {len(script)} chars")
        
        # Split script into chunks for parallel processing
        chunks = _split_script_into_chunks(script, max_chars=500)
        num_chunks = len(chunks)
        
        logger.info(f"📝 Script: {len(script)} chars → {num_chunks} chunk(s)")
        for i, chunk in enumerate(chunks):
            logger.info(f"  Chunk {i+1}/{num_chunks}: {len(chunk)} chars")
        
        if num_chunks == 1:
            # ── SINGLE CHUNK: Direct call (no overhead) ──────────────────
            logger.info(f"📞 Single chunk → direct HeyGen API call")
            result = _call_heygen_tts_chunk(chunks[0], voice.voice_id, heygen_api_key, 0)
            
            if 'error' in result:
                err = result['error']
                # Auto-remove invalid voice when HeyGen returns "Voice not found"
                if 'Voice not found' in err:
                    Voice.objects.filter(voice_id=voice.voice_id).delete()
                    logger.warning(f"Removed invalid voice from DB: {voice.voice_id}")
                    err = (
                        "Voice not found on HeyGen. Invalid voice removed. "
                        "Refresh page and select another voice (e.g. HuyK)."
                    )
                return Response(
                    {'error': err},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            audio_url = result['audio_url']
            
            # Download and cache locally
            if _download_audio_file(audio_url, cache_path):
                elapsed = time_module.time() - start_time
                logger.info(f"✅ Audio cached locally ({elapsed:.1f}s, key={cache_key[:12]})")
                audio_serve_url = request.build_absolute_uri(f'/api/audio/cache/{cache_key}.mp3')
            else:
                # Fallback to HeyGen URL if download fails
                audio_serve_url = audio_url
                elapsed = time_module.time() - start_time
                logger.warning(f"⚠️ Cache download failed, using HeyGen URL ({elapsed:.1f}s)")
            
        else:
            # ── MULTI CHUNK: Parallel processing ─────────────────────────
            logger.info(f"🚀 {num_chunks} chunks → PARALLEL HeyGen API calls")
            
            chunk_results = [None] * num_chunks
            
            # Use ThreadPoolExecutor for parallel API calls
            with ThreadPoolExecutor(max_workers=min(num_chunks, 4)) as executor:
                futures = {}
                for i, chunk in enumerate(chunks):
                    future = executor.submit(
                        _call_heygen_tts_chunk,
                        chunk, voice.voice_id, heygen_api_key, i
                    )
                    futures[future] = i
                
                for future in as_completed(futures):
                    idx = futures[future]
                    chunk_results[idx] = future.result()
            
            # Check for errors
            errors = [r for r in chunk_results if r and 'error' in r]
            if errors:
                error_msg = "; ".join([e['error'] for e in errors[:3]])
                # Auto-remove invalid voice when HeyGen returns "Voice not found"
                if 'Voice not found' in error_msg:
                    Voice.objects.filter(voice_id=voice.voice_id).delete()
                    logger.warning(f"Removed invalid voice from DB: {voice.voice_id}")
                    error_msg = (
                        "Voice not found on HeyGen. Invalid voice removed. "
                        "Refresh page and select another voice (e.g. HuyK)."
                    )
                else:
                    error_msg = f'Audio generation partially failed: {error_msg}'
                logger.error(f"❌ {len(errors)}/{num_chunks} chunks failed: {error_msg}")
                return Response(
                    {'error': error_msg},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Download all chunk audio files
            temp_dir = tempfile.mkdtemp(prefix='audio_chunks_')
            chunk_paths = []
            
            try:
                download_ok = True
                for i, result in enumerate(chunk_results):
                    chunk_file = os.path.join(temp_dir, f"chunk_{i:03d}.mp3")
                    if not _download_audio_file(result['audio_url'], chunk_file):
                        download_ok = False
                        break
                    chunk_paths.append(chunk_file)
                
                if not download_ok or len(chunk_paths) != num_chunks:
                    return Response(
                        {'error': 'Failed to download audio chunks'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
                
                # Merge all chunks into one file
                if not _merge_audio_files(chunk_paths, cache_path):
                    return Response(
                        {'error': 'Failed to merge audio chunks'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
                
                elapsed = time_module.time() - start_time
                logger.info(
                    f"✅ {num_chunks} chunks merged + cached ({elapsed:.1f}s, "
                    f"key={cache_key[:12]})"
                )
                
            finally:
                # Cleanup temp files
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            
            audio_serve_url = request.build_absolute_uri(f'/api/audio/cache/{cache_key}.mp3')
        
        elapsed = time_module.time() - start_time
        return Response({
            'success': True,
            'audio_url': audio_serve_url,
            'voice_name': voice.name,
            'provider': 'heygen',
            'cached': False,
            'chunks': num_chunks,
            'elapsed': round(elapsed, 2)
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Generate audio error: {e}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def serve_cached_audio(request, filename: str):
    """Serve cached audio files from AUDIO_CACHE_DIR."""
    from django.http import FileResponse

    # Sanitize filename to prevent path traversal
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(AUDIO_CACHE_DIR, safe_filename)

    if not os.path.isfile(file_path):
        return Response(
            {'error': 'Audio file not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    return FileResponse(
        open(file_path, 'rb'),
        content_type='audio/mpeg',
        filename=safe_filename
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
        "use_a4_formula": true  // Optional: Use A4 V3 formula (7 simple slots, no split layout)
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
        
        # Handle forced product video (from SKU search)
        forced_product_video_id = request.POST.get('forced_product_video_id')
        if forced_product_video_id:
            try:
                forced_product_video_id = int(forced_product_video_id)
                logger.info(f"🔒 Forced product video ID: {forced_product_video_id}")
            except ValueError:
                logger.warning(f"Invalid forced_product_video_id: {forced_product_video_id}")
                forced_product_video_id = None
        
        # Get product id, category and SKU for auto-indexing
        product_id = request.POST.get('product_id')
        product_category = request.POST.get('product_category')
        product_sku = request.POST.get('product_sku')
        
        if product_id:
            logger.info(f"🧾 Product ID: {product_id}")
        if product_category:
            logger.info(f"📦 Product category: {product_category}")
        if product_sku:
            logger.info(f"🏷️ Product SKU: {product_sku}")
        
        # Create progress tracking (Redis-backed, survives server restart)
        progress_id = uuid.uuid4().hex
        progress_set(progress_id, {
            "status": "processing",
            "percent": 0,
            "message": "Queued — waiting for available slot...",
            "num_outputs": num_outputs,
            "error": None,
            "output_urls": None,
            "output_filenames": None,
        })

        # Check concurrent mix limit BEFORE starting thread
        # Nếu đã đạt giới hạn → thông báo user xếp hàng (không block request)
        active_slots = _MIX_MAX_CONCURRENT - _mix_semaphore._value  # số job đang chạy
        if active_slots >= _MIX_MAX_CONCURRENT:
            logger.warning(f"⚠️ Mix queue full ({active_slots}/{_MIX_MAX_CONCURRENT}). Job {progress_id} will wait.")

        # Start mix task in background
        # daemon=False: thread không bị kill khi main process nhận SIGTERM
        # → mix vẫn hoàn thành ngay cả khi server đang reload
        is_temp_audio = temp_audio is not None
        t = threading.Thread(
            target=_run_smart_mix_task_with_semaphore,
            args=(
                progress_id,
                audio_path,
                num_outputs,
                width,
                height,
                use_gpu,
                is_temp_audio,
                use_a4_formula,
                forced_product_video_id,
                product_id,
                product_category,
                product_sku
            ),
            daemon=False,
            name=f"smart-mix-{progress_id[:8]}"
        )
        t.start()
        
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
    """Get mix progress status (reads from Redis, survives restart)."""
    data = progress_get(progress_id)
    if data is None:
        return Response(
            {'error': 'Progress ID not found. The server may have restarted.'},
            status=status.HTTP_404_NOT_FOUND
        )
    return Response(data)


def _run_smart_mix_task_with_semaphore(*args, **kwargs):
    """
    Wrapper: acquire semaphore trước khi chạy mix → giới hạn concurrent jobs.
    Nếu 3 job đang chạy, job thứ 4 sẽ đợi (không reject).
    """
    progress_id = args[0]
    progress_update(progress_id, {"message": "Waiting for available slot..."})
    with _mix_semaphore:  # Block ở đây nếu đã full
        progress_update(progress_id, {"message": "Initializing..."})
        _run_smart_mix_task(*args, **kwargs)


def _run_smart_mix_task(
    progress_id: str,
    audio_path: str,
    num_outputs: int,
    width: int,
    height: int,
    use_gpu: Optional[bool],
    is_temp_audio: bool,
    use_a4_formula: bool = False,
    forced_product_video_id: Optional[int] = None,
    product_id: Optional[str] = None,
    product_category: Optional[str] = None,
    product_sku: Optional[str] = None
):
    """Background task for smart mix."""
    service = get_preprocessing_service()
    output_dir = os.path.join(settings.MEDIA_ROOT, 'mix_outputs', progress_id)
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Update progress
        _update_progress(progress_id, 5, "Preparing...")

        # Resolve product info from catalog if we have product_id and/or product_sku
        try:
            catalog_product = None

            if product_id:
                try:
                    catalog_product = Product.objects.filter(id=int(product_id)).first()
                except (ValueError, TypeError):
                    logger.warning(f"⚠️ Invalid product_id received: {product_id}")

            # Fallback: lookup by SKU if no product found yet
            if not catalog_product and product_sku:
                catalog_product = Product.objects.filter(sku__iexact=product_sku.strip()).order_by('-created_at').first()

            if catalog_product:
                # Prefer catalog values when FE did not send them
                if not product_category or not product_category.strip():
                    if catalog_product.category:
                        product_category = catalog_product.category
                        logger.info(f"📦 Inferred product category '{product_category}' from catalog (product id={catalog_product.id})")
                if not product_sku and catalog_product.sku:
                    product_sku = catalog_product.sku
                    logger.info(f"🏷️ Using SKU '{product_sku}' from catalog (product id={catalog_product.id})")
                
                logger.info(
                    f"✅ Product resolved for MIX pipeline: "
                    f"id={catalog_product.id}, name='{catalog_product.name}', "
                    f"category='{product_category}', sku='{product_sku}'"
                )
            else:
                if product_id:
                    logger.warning(f"⚠️ No Product found in catalog for id={product_id}")
                elif product_sku:
                    logger.warning(f"⚠️ No Product found in catalog for SKU '{product_sku}'")
        except Exception as e:
            logger.error(f"Error while resolving product info from catalog (id={product_id}, sku={product_sku}): {e}", exc_info=True)
        
        # Get audio duration ONCE (all outputs will have same duration!)
        audio_duration = _get_media_duration(audio_path)
        if not audio_duration:
            logger.error(f"Failed to get audio duration: {audio_path}")
            audio_duration = 60  # Default fallback
        
        logger.info(f"🎵 Audio duration: {audio_duration}s (all outputs will match this)")
        # -------------------------------------------------------------
        # AUTO-INDEX OUTRO (If missing)
        # -------------------------------------------------------------
        from video_management.models import IndexedVideo
        if not IndexedVideo.objects.filter(folder_type="Outtrol").exists():
             logger.info("🔍 'Outtrol' index missing. Auto-scanning...")
             _auto_index_outro(service)

        # -------------------------------------------------------------
        # AUTO-INDEX BY SKU (First Priority)
        # Xóa Sản phẩm/Chế tác cũ rồi index theo SKU → chỉ video của sản phẩm đang chọn
        # -------------------------------------------------------------   
        sku_found = False
        if product_sku:
             result = _auto_index_by_sku_global(service, product_sku, product_category)
             sku_found = bool(result)
             
        # -------------------------------------------------------------
        # AUTO-INDEX CATEGORY FOLDER (Fallback if SKU not found)
        # Chế tác không fallback folder tổng — chỉ index theo mã SKU.
        # -------------------------------------------------------------
        if not sku_found and product_category:
            _auto_index_category_folder(service, product_category)
        
        # Calculate flexible slot duration for A4 (slot 1-6 share audio duration)
        slot_duration = audio_duration / 6 if use_a4_formula else None
        
        # Check if we have enough indexed videos for the selected mode
        if use_a4_formula:
            # STRICT VALIDATION FOR A4 V3: ALL 7 SLOTS MUST HAVE VIDEOS!
            from video_management.models import IndexedVideo
            
            logger.info("🔍 Validating A4 Formula V3 requirements (7 simple slots, flexible duration)...")
            logger.info(f"🎵 Audio: {audio_duration}s → Slot 1-6: {slot_duration:.2f}s each, Slot 7: original")
            logger.info("✨ Note: Simple fullscreen videos only (NO split layouts)")
            
            missing_slots = []
            
            for i, slot in enumerate(A4_FORMULA, start=1):
                folder_type = slot.get('folder_type')
                is_flexible = slot.get('flexible', False)
                required_duration = slot_duration if is_flexible else slot.get('duration', 3)
                
                # Validation: chỉ cần folder có ít nhất 1 video (không check duration)
                # Auto-fill sẽ tự concat nhiều clip ngắn nếu video ngắn hơn required_duration
                count = IndexedVideo.objects.filter(
                    folder_type=folder_type,
                    is_available=True
                ).count()
                
                if count == 0:
                    missing_slots.append(f"Slot {i}: {folder_type}")
                    logger.error(f"❌ Slot {i}/7: {folder_type} - KHÔNG CÓ VIDEO NÀO ĐƯỢC INDEX!")
                else:
                    # Log thêm thông tin về video đủ dài
                    count_ok = IndexedVideo.objects.filter(
                        folder_type=folder_type,
                        is_available=True,
                        duration__gte=required_duration
                    ).count()
                    if count_ok > 0:
                        logger.info(f"✅ Slot {i}/7: {folder_type} - {count_ok}/{count} videos >= {required_duration:.1f}s")
                    else:
                        logger.warning(
                            f"⚠️ Slot {i}/7: {folder_type} - {count} videos nhưng KHÔNG CÓ video nào >= {required_duration:.1f}s. "
                            f"Auto-fill sẽ ghép nhiều clip ngắn lại."
                        )
            
            if missing_slots:
                error_details = "\n".join(missing_slots)
                logger.error(f"\n⚠️ A4 V3 VALIDATION FAILED!\nMissing slots:\n{error_details}")
                logger.error("\n💡 Solution: Go to 'Quản lý Folders' and index ALL required folder types!")
                
                raise ValueError(
                    f"❌ A4 Formula V3 requires all slots to have videos!\n\n"
                    f"Missing slots:\n{error_details}\n\n"
                    f"⚠️ ESPECIALLY CHECK:\n"
                    f"- Slot 7: 'Outtrol' (Outro with original audio)\n\n"
                    f"Note: Slot 1 'Sản phẩm' will be auto-indexed if you provide product SKU/category."
                )
            
            logger.info(f"✅ A4 V3 Validation passed! All required slots have videos.")
        else:
            # Quick validation for random mode
            video_dict = service.get_random_videos(FOLDER_TYPES, product_category=product_category)
            available_count = sum(1 for v in video_dict.values() if v is not None)
            if available_count < 5:
                raise ValueError(f"Not enough videos indexed. Only {available_count} folder(s) have videos. Need at least 5!")
        
        # ── PHASE 1: Chọn videos cho tất cả outputs ──────────────────────────
        # globally_used: track video đã dùng theo từng folder_type giữa các outputs
        # → đảm bảo 5 outputs khác nhau (đặc biệt slot Sản phẩm)
        _update_progress(progress_id, 12, "Selecting videos for all outputs...")
        all_selections = []
        globally_used = {}  # { folder_type: set(video_ids) }
        
        for i in range(num_outputs):
            if use_a4_formula:
                sel = _get_a4_formula_videos(
                    service, slot_duration,
                    product_category=product_category,
                    product_sku=product_sku,
                    globally_used=globally_used
                )
                # Cập nhật globally_used sau mỗi output
                for j, slot in enumerate(A4_FORMULA):
                    ft = slot['folder_type']
                    if j < len(sel) and sel[j] is not None:
                        globally_used.setdefault(ft, set()).add(sel[j])
                logger.info(f"Output {i+1}: A4 V3 formula selected ({len(sel)} slots)")
            else:
                vd = service.get_random_videos(FOLDER_TYPES, product_category=product_category)
                sel = list(vd.values())
                logger.info(f"Output {i+1}: Random mode ({len([v for v in sel if v])} videos)")
            all_selections.append(sel)

        # ── PHASE 2: Pre-warm clip cache (SONG SONG) ──────────────────────────
        # Thu thập tất cả unique video IDs cần generate clip
        # Các outputs khác nhau → chọn video khác nhau → ít share cache
        # Nhưng CÙNG output: clip của slot X được dùng cho output 1
        # Key insight: pre-generate tất cả clips trước, rồi mix sẽ là cache HIT
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        # Collect (video_id, clip_duration) cần pre-warm từ tất cả selections
        # Dùng set để dedup (cùng video_id + duration → chỉ generate 1 lần)
        prewarm_tasks = set()  # (video_id, clip_duration_rounded)
        for i, sel in enumerate(all_selections):
            for j, video_id in enumerate(sel):
                if video_id is None:
                    continue
                if use_a4_formula and j < len(A4_FORMULA):
                    slot_cfg = A4_FORMULA[j]
                    is_outro = slot_cfg.get('use_original_audio', False)
                    if is_outro:
                        # Outro duration cần query DB - skip pre-warm (nhanh vì cache)
                        continue
                    dur = slot_duration if slot_cfg.get('flexible') else slot_cfg.get('duration', 8)
                else:
                    dur = 8
                # Round duration để dedup tốt hơn
                prewarm_tasks.add((video_id, round(dur, 2)))
        
        if prewarm_tasks:
            logger.info(f"🚀 Pre-warming {len(prewarm_tasks)} unique clips in parallel...")
            _update_progress(progress_id, 15, f"Pre-generating {len(prewarm_tasks)} clips in parallel...")
            
            def _prewarm_clip(task):
                vid_id, dur = task
                try:
                    result = service.get_or_generate_clip(vid_id, use_gpu, duration=dur)
                    if result:
                        logger.info(f"  ✅ Pre-warmed clip: video={vid_id}, dur={dur}s")
                    else:
                        logger.warning(f"  ⚠️ Pre-warm failed: video={vid_id}, dur={dur}s")
                    return vid_id, result
                except Exception as e:
                    logger.error(f"  ❌ Pre-warm error video={vid_id}: {e}")
                    return vid_id, None
            
            # ⚡ Tối đa 8 workers song song — ffmpeg clip generation là I/O bound (NAS)
            # Căng thẳng network ít hơn so với CPU → 8 workers an toàn
            max_workers_prewarm = min(8, len(prewarm_tasks))
            with ThreadPoolExecutor(max_workers=max_workers_prewarm, thread_name_prefix="prewarm") as executor:
                futures = {executor.submit(_prewarm_clip, t): t for t in prewarm_tasks}
                done_count = 0
                for future in as_completed(futures):
                    done_count += 1
                    pct = 15 + int(done_count / len(prewarm_tasks) * 50)  # 15% → 65%
                    _update_progress(progress_id, pct, f"Pre-generating clips... ({done_count}/{len(prewarm_tasks)})")
            
            logger.info(f"✅ Pre-warm complete. Cache is hot for mix phase.")

        # ── PHASE 3: Mix tất cả outputs (SONG SONG) ──────────────────────────
        _update_progress(progress_id, 67, f"Mixing {num_outputs} videos in parallel...")
        logger.info(f"🎬 Mixing {num_outputs} outputs in parallel (clips are pre-cached)...")
        
        output_files_map = {}  # index → path (để giữ thứ tự)
        mix_lock = threading.Lock()
        
        def _mix_one(i):
            sel = all_selections[i]
            try:
                f = _generate_one_mix(
                    progress_id, i, sel,
                    audio_path, audio_duration,
                    width, height, use_gpu, service, output_dir,
                    use_a4_formula, slot_duration, forced_product_video_id
                )
                with mix_lock:
                    _update_progress(
                        progress_id,
                        67 + int((len(output_files_map) + 1) / num_outputs * 30),
                        f"Mixed {len(output_files_map)+1}/{num_outputs} videos..."
                    )
                return i, f
            except Exception as e:
                logger.error(f"❌ Mix output {i+1} failed: {e}", exc_info=True)
                return i, None
        
        # ⚡ Tối đa 5 workers — clips đã có trong cache → mix chỉ cần concat + replace audio (I/O)
        # Không tạo clip mới (pre-warm đã xong) → không có GPU conflict
        max_workers_mix = min(5, num_outputs)
        with ThreadPoolExecutor(max_workers=max_workers_mix, thread_name_prefix="mix") as executor:
            mix_futures = [executor.submit(_mix_one, i) for i in range(num_outputs)]
            for future in as_completed(mix_futures):
                idx, path = future.result()
                if path:
                    output_files_map[idx] = path
        
        # Giữ nguyên thứ tự output
        output_files = [output_files_map[i] for i in range(num_outputs) if i in output_files_map]
        
        # Generate URLs - Use full URL with AI service host
        ai_service_url = os.getenv('AI_SERVICE_URL', 'http://localhost:8001')
        output_urls = [
            f"{ai_service_url}{settings.MEDIA_URL}mix_outputs/{progress_id}/{os.path.basename(f)}"
            for f in output_files
        ]
        
        output_filenames = [os.path.basename(f) for f in output_files]
        
        # Complete
        progress_update(progress_id, {
            "status": "completed",
            "percent": 100,
            "message": f"Generated {len(output_files)} videos",
            "output_urls": output_urls,
            "output_filenames": output_filenames,
        })
        logger.info(f"✅ Smart mix completed: {progress_id} ({len(output_files)} videos)")
    
    except Exception as e:
        logger.error(f"Smart mix task error: {e}", exc_info=True)
        progress_update(progress_id, {
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
    slot_duration: Optional[float] = None,  # For A4 V3 flexible slots
    forced_product_video_id: Optional[int] = None
) -> Optional[str]:
    """
    Generate one mixed video using cached clips.
    
    For A4 V3:
    - Slot 1-6: Use slot_duration (flexible, based on audio)
    - Slot 7 (Outro): Use original duration + original audio
    """
    
    # Validate forced product video (có thể đã bị xóa khi clear index hoặc không còn available)
    if forced_product_video_id:
        try:
            exists = IndexedVideo.objects.filter(
                id=forced_product_video_id,
                is_available=True
            ).exists()
        except Exception:
            exists = False

        if not exists:
            logger.warning(
                f"⚠️ Forced product video ID {forced_product_video_id} not found or unavailable. "
                f"Ignoring forced selection for this mix."
            )
            forced_product_video_id = None

    logger.info(f"Output {output_index}: Target duration = {audio_duration}s" + 
                (f", Slot duration = {slot_duration:.2f}s" if slot_duration else ""))
    
    # Get/generate clips for each slot
    clip_paths = []
    clip_durations = []
    outro_clip_path = None  # For A4 V3: Outro with original audio
    outro_duration = None
    
    for i, video_selection in enumerate(video_selections):
        # Check if this is Outro slot (last slot in A4 V3)
        is_outro = (use_a4_formula and i == len(video_selections) - 1 and 
                    i < len(A4_FORMULA) and A4_FORMULA[i].get('use_original_audio'))
        
        # A4 V3: All slots are simple videos (no SPLIT layouts)
        video_id = video_selection
        
        # --- FORCE PRODUCT VIDEO LOGIC ---
        if forced_product_video_id:
            # Check if this slot is "Sản phẩm"
            current_folder_type = None
            if use_a4_formula and i < len(A4_FORMULA):
                current_folder_type = A4_FORMULA[i].get('folder_type')
            elif i < len(FOLDER_TYPES):
                current_folder_type = FOLDER_TYPES[i]
            
            if current_folder_type == "Sản phẩm":
                video_id = forced_product_video_id
                logger.info(f"🔒 Output {output_index}: FORCED specific video for 'Sản phẩm' slot (ID {video_id})")
        # ---------------------------------
        
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
            # Outro: Giữ nguyên âm thanh gốc (không re-encode audio)
            clip_path = service.get_or_generate_clip(
                video_id, use_gpu, duration=clip_duration, keep_original_audio=True
            )
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
        logger.info(f"  Clip {idx}: {os.path.basename(clip)} ({dur:.2f}s)")
    if outro_clip_path:
        logger.info(f"  Outro: {os.path.basename(outro_clip_path)} ({outro_duration:.2f}s) [ORIGINAL AUDIO]")
    
    # Validation
    if use_a4_formula:
        expected_content_clips = 6  # Slot 1-6
        if len(clip_paths) != expected_content_clips:
            raise ValueError(f"❌ A4 V3 Formula requires {expected_content_clips} content clips! Got {len(clip_paths)} clips.")
        if not outro_clip_path:
            raise ValueError(f"❌ A4 V3 Formula: Missing Outro (Slot 7)!")
    elif len(clip_paths) < 5:
        raise ValueError(f"Not enough clips generated: {len(clip_paths)} clips. Need at least 5 clips to mix.")
    
    # Calculate total content duration (Slot 1-6)
    total_content_duration = sum(clip_durations)
    logger.info(f"📊 Content duration (Slot 1-6): {total_content_duration:.2f}s")
    
    # For A4 V3: Content should match audio_duration (Slot 1-6 only)
    # No need to loop - slot_duration is calculated to fill audio_duration exactly
    if use_a4_formula and outro_clip_path:
        logger.info(f"🎵 A4 V3 Mode:")
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
    
    # If A4 V3: Concat content + outro (outro keeps original audio)
    if use_a4_formula and outro_clip_path:
        logger.info("🔗 Concatenating content + outro...")
        final_concat_success = _concat_clips_fast([content_with_audio, outro_clip_path], output_file)
        
        # Cleanup temp content with audio
        if os.path.exists(content_with_audio):
            os.remove(content_with_audio)
        
        if final_concat_success:
            logger.info(f"✅ A4 V3 Final output: {os.path.basename(output_file)}")
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

        # ⚡ Timeout rõ ràng: 60s đủ để copy — nếu quá tức là bị block
        concat_copy_timeout = 60 + len(clip_paths) * 5  # 60s base + 5s/clip
        try:
            result = subprocess.run(
                cmd_fast,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=concat_copy_timeout
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"⏱️ Concat copy TIMEOUT ({concat_copy_timeout}s) — falling back to re-encode")
            if os.path.exists(output_path):
                os.remove(output_path)
            result = type('R', (), {'returncode': 1, 'stderr': 'timeout'})()
        
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
        
        # ⚡ Re-encode timeout: 60s base + 30s/clip — tránh chờ vô hạn
        reencode_timeout = 60 + len(clip_paths) * 30
        try:
            result2 = subprocess.run(
                cmd_reencode,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=reencode_timeout
            )
        except subprocess.TimeoutExpired:
            logger.error(f"❌ Concat re-encode TIMEOUT ({reencode_timeout}s)!")
            if os.path.exists(list_file):
                os.remove(list_file)
            if os.path.exists(output_path):
                os.remove(output_path)
            return False
        
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
    """Update progress tracking (Redis-backed)."""
    if progress_exists(progress_id):
        progress_update(progress_id, {"percent": percent, "message": message})


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


def _get_a4_formula_videos(
    service, 
    slot_duration, 
    product_category: Optional[str] = None,
    product_sku: Optional[str] = None,
    globally_used: Optional[Dict[str, set]] = None
):
    """
    Get videos for A4 Formula V3 (flexible duration, 7 simple slots).
    
    DEDUP LOGIC:
    1. Trong 1 video: HuyK slot2 ≠ HuyK slot4, Chế tác slot3 ≠ Chế tác slot5
       (tracked with locally_used per folder_type)
    2. Giữa các outputs: ưu tiên video CHƯA DÙNG ở outputs trước (globally_used)
       Đặc biệt ưu tiên slot Sản phẩm khác nhau giữa 5 outputs
    3. Fallback: nếu hết video mới → cho phép dùng lại (tránh crash)
    
    Args:
        service: SmartPreprocessingService instance
        slot_duration: Duration for flexible slots (1-6)
        product_category: Optional string to filter videos in "Sản phẩm" folders
        globally_used: Dict { folder_type: set(video_ids) } đã dùng ở các outputs trước
    
    Returns:
        List of video IDs (integers) — 7 slots
    """
    from video_management.models import IndexedVideo
    import random
    from django.db.models import Q
    
    if globally_used is None:
        globally_used = {}
    
    selected_videos = []
    missing_slots = []
    
    # Track video đã chọn TRONG output này theo folder_type
    # → đảm bảo HuyK slot2 ≠ slot4, Chế tác slot3 ≠ slot5
    locally_used = {}  # { folder_type: set(video_ids) }
    
    for i, slot in enumerate(A4_FORMULA, start=1):
        folder_type = slot.get('folder_type')
        is_flexible = slot.get('flexible', False)
        required_duration = slot_duration if is_flexible else slot.get('duration', 3)
        
        # Tập hợp video cần TRÁNH:
        # - locally_used: video đã chọn trong cùng output (tránh HuyK slot2 == slot4)
        # - globally_used: video đã chọn ở outputs trước (tránh 5 outputs giống nhau)
        local_exclude = locally_used.get(folder_type, set())
        global_exclude = globally_used.get(folder_type, set())
        all_exclude = local_exclude | global_exclude
        
        video_id = _pick_video_for_slot(
            folder_type=folder_type,
            required_duration=required_duration,
            product_category=product_category,
            product_sku=product_sku,
            exclude_ids=all_exclude,
            fallback_exclude_ids=local_exclude,  # Ít nhất tránh trùng TRONG output
        )
        
        if video_id:
            selected_videos.append(video_id)
            locally_used.setdefault(folder_type, set()).add(video_id)
            
            if slot.get('use_original_audio'):
                logger.info(f"✅ A4 Slot {i}/7: {folder_type} (OUTRO) → Video {video_id}")
            else:
                logger.info(f"✅ A4 Slot {i}/7: {folder_type} ({required_duration:.2f}s) → Video {video_id}")
        else:
            logger.error(f"❌ A4 Slot {i}/7: {folder_type} ({required_duration:.2f}s) → NO VIDEO FOUND!")
            selected_videos.append(None)
            missing_slots.append(f"Slot {i}: {folder_type}")
    
    # CRITICAL VALIDATION
    if missing_slots:
        error_msg = f"❌ A4 Formula V3 FAILED! Missing slots:\n" + "\n".join(missing_slots)
        logger.error(error_msg)
        raise ValueError(error_msg + "\n\nPlease index ALL required folder types!")
    
    logger.info(f"✅ A4 Formula V3 complete: Selected 7/7 slots successfully!")
    return selected_videos


def _pick_video_for_slot(
    folder_type: str,
    required_duration: float,
    product_category: Optional[str] = None,
    product_sku: Optional[str] = None,
    exclude_ids: Optional[set] = None,
    fallback_exclude_ids: Optional[set] = None,
):
    """
    Chọn 1 video cho 1 slot, ưu tiên:
    1. Video khớp category + chưa dùng (exclude_ids) + duration đủ dài
    2. Video khớp category + chưa dùng (exclude_ids) + bất kỳ duration
    3. Video bất kỳ + chưa dùng (exclude_ids) + duration đủ dài
    4. Video bất kỳ + chưa dùng (fallback_exclude_ids) — chỉ tránh trùng trong output
    5. Video bất kỳ — cho phép dùng lại (tránh crash)
    
    Returns: video_id (int) hoặc None
    """
    from video_management.models import IndexedVideo
    import random
    from django.db.models import Q
    
    if exclude_ids is None:
        exclude_ids = set()
    if fallback_exclude_ids is None:
        fallback_exclude_ids = set()
    
    base_query = Q(folder_type=folder_type, is_available=True)
    
    def _pick_from_qs(qs):
        """Random pick từ QuerySet, trả None nếu rỗng."""
        ids = list(qs.values_list('id', flat=True))
        return random.choice(ids) if ids else None
    
    # ── LEVEL 1: product_sku (ưu tiên) hoặc product_category cho Sản phẩm, Sản phẩm HT, Chế tác ──
    path_filter = None
    if product_sku and folder_type in ["Sản phẩm", "Sản phẩm HT", "Chế tác"]:
        # Lọc theo SKU trong đường dẫn: chỉ dùng video của sản phẩm đã chọn
        sku_safe = product_sku.strip().replace("\\", "/")
        path_filter = Q(file_path__icontains=sku_safe)
    elif product_category and folder_type in ["Sản phẩm", "Sản phẩm HT", "Chế tác"]:
        path_filter = Q(file_path__icontains=product_category)

    if path_filter:
        qs = IndexedVideo.objects.filter(
            base_query & path_filter & Q(duration__gte=required_duration)
        ).exclude(id__in=exclude_ids)
        vid = _pick_from_qs(qs)
        if vid:
            return vid

        qs = IndexedVideo.objects.filter(
            base_query & path_filter
        ).exclude(id__in=exclude_ids)
        vid = _pick_from_qs(qs)
        if vid:
            return vid
    
    # ── LEVEL 2: Không filter category + exclude_ids + đủ dài ──
    qs = IndexedVideo.objects.filter(
        base_query & Q(duration__gte=required_duration)
    ).exclude(id__in=exclude_ids)
    vid = _pick_from_qs(qs)
    if vid:
        return vid
    
    # ── LEVEL 3: exclude_ids + bất kỳ duration ──
    qs = IndexedVideo.objects.filter(base_query).exclude(id__in=exclude_ids)
    vid = _pick_from_qs(qs)
    if vid:
        return vid
    
    # ── LEVEL 4: Chỉ tránh trùng TRONG output (fallback_exclude_ids) ──
    if fallback_exclude_ids != exclude_ids:
        qs = IndexedVideo.objects.filter(base_query).exclude(id__in=fallback_exclude_ids)
        vid = _pick_from_qs(qs)
        if vid:
            logger.warning(f"  ⚠️ '{folder_type}': Hết video mới giữa outputs, dùng lại (chỉ tránh trùng trong output)")
            return vid
    
    # ── LEVEL 5: Cho phép dùng lại tất cả (ultimate fallback) ──
    qs = IndexedVideo.objects.filter(base_query)
    vid = _pick_from_qs(qs)
    if vid:
        logger.warning(f"  ⚠️ '{folder_type}': Chỉ có 1 video, phải dùng lại!")
    return vid




def _auto_index_category_folder(service, category_name: str):
    """
    Automatically find and index the folder corresponding to the category.
    E.g. Category="Dây chuyền" -> Search in Generate Video\Video Sản Phẩm\Dây chuyền
    """
    try:
        if not category_name:
            return

        logger.info(f"🔍 Auto-indexing category: '{category_name}'")
        
        # Look inside Generate Video\Video Sản Phẩm
        base_paths = [
            r"\\VCB_MEDIA\MEDIA VCB folder\Generate Video\Video Sản Phẩm",
        ]
        
        target_path = None
        
        for base in base_paths:
            if not os.path.exists(base):
                continue
                
            target_path = service.find_folder_by_name(
                root_path=base,
                target_name=category_name.strip(),
                exact_match=False,
                max_depth=2
            )
            
            if target_path:
                break
        
        if target_path and os.path.isdir(target_path):
            logger.info(f"✅ Found category folder: '{target_path}'")
            service.index_videos_from_folders({"Sản phẩm": target_path})
        else:
            logger.warning(f"⚠️ Could not find auto-folder for category '{category_name}'")
            
    except Exception as e:
        logger.error(f"Auto-index error: {e}")


def _auto_index_by_sku(service, sku: str):
    """
    Scan VIDEO_BASE_PATHS/PRODUCT_VIDEO_SUBFOLDER for a subfolder matching the SKU.
    If found, index ALL videos in that folder into 'Sản phẩm'.
    """
    try:
        if not sku: return False

        logger.info(f"🕵️ Searching for folder with SKU: '{sku}'")
        sku_clean = sku.strip().lower()
        
        # Use settings paths (from .env) instead of hardcoded paths
        base_paths = getattr(settings, 'PRODUCT_VIDEO_PATHS', [])
        if not base_paths:
            # Build from VIDEO_BASE_PATHS + PRODUCT_VIDEO_SUBFOLDER
            # .env: VIDEO_BASE_PATHS=//VCB_MEDIA/MEDIA VCB folder/Generate Video
            #       PRODUCT_VIDEO_SUBFOLDER=Video Sản Phẩm
            video_bases = getattr(settings, 'VIDEO_BASE_PATHS', [])
            subfolder = getattr(settings, 'PRODUCT_VIDEO_SUBFOLDER', 'Video Sản Phẩm')
            base_paths = [os.path.join(b, subfolder) for b in video_bases]
        
        logger.info(f"📂 Scanning {len(base_paths)} product paths for SKU '{sku}': {base_paths}")
        
        # Search ALL base paths (not just the first one)
        for search_root in base_paths:
            if not os.path.isdir(search_root):
                logger.warning(f"⚠️ Path not accessible: {search_root}")
                continue
                
            target_path = service.find_folder_by_name(
                 root_path=search_root,
                 target_name=sku_clean,
                 exact_match=False,
                 max_depth=4
            )
                
            if target_path:
                logger.info(f"✅ Found specific product folder for SKU '{sku}': {target_path}")
                # Index ALL videos into BOTH Slot 1 (Sản phẩm) AND Slot 6 (Sản phẩm HT)
                # Both slots show product-specific videos of the chosen SKU
                results = service.index_videos_from_folders({
                    "Sản phẩm": target_path,
                    "Sản phẩm HT": target_path,
                })
                count_sp = results.get("Sản phẩm", 0)
                count_ht = results.get("Sản phẩm HT", 0)
                
                logger.info(f"📊 SKU '{sku}': Slot 1 (Sản phẩm) +{count_sp} new | Slot 6 (Sản phẩm HT) +{count_ht} new")
                return True
        
        logger.warning(f"⚠️ Could not find folder for SKU '{sku}' in any product path: {base_paths}")
        return False
        
    except Exception as e:
        logger.error(f"Auto-index SKU error: {e}", exc_info=True)
        return False


@api_view(['POST'])
def index_manufacturing_folder(request):
    """
    Manually trigger indexing for a specific manufacturing folder (Category + SKU).
    Useful when frontend detects a new product selection.
    
    POST Body:
    {
        "category": "Dây chuyền",
        "sku": "MD64"
    }
    """
    try:
        category = request.data.get('category', '')
        sku = request.data.get('sku', '')
        
        logger.info(f"🔧 Manual trigger: Index manufacturing folder. Cat='{category}', SKU='{sku}'")
        
        if not category and not sku:
             return Response({'error': 'Category or SKU required'}, status=status.HTTP_400_BAD_REQUEST)

        service = get_preprocessing_service()
        if sku:
            # Index Product + Chế tác theo đúng mã SKU
            _auto_index_by_sku_global(service, sku, category)
            return Response({'success': True, 'message': f'Indexed for SKU={sku}, Cat={category}'})
        # Không fallback folder tổng — Chế tác bắt buộc theo SKU
        return Response({
            'success': False,
            'message': 'Chế tác cần mã SKU. Vui lòng truyền sku để index theo đúng sản phẩm.'
        }, status=status.HTTP_400_BAD_REQUEST)
        
    except Exception as e:
        logger.error(f"Error indexing manufacturing folder: {e}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _auto_index_outro(service):
    """
    Find and index "Outtrol" (Outro) folder.
    Primary location: \\VCB_MEDIA\MEDIA VCB folder\SOURCE HUYK\OUTRO HUYK
    Fallback: scan for any folder containing 'outro' (case-insensitive).
    """
    try:
        logger.info("🔍 Auto-scanning for Outro folder...")
        
        # Primary: direct confirmed path
        primary_path = r"\\VCB_MEDIA\MEDIA VCB folder\SOURCE HUYK\OUTRO HUYK"
        if os.path.isdir(primary_path):
            logger.info(f"✅ Found Outro folder (primary): {primary_path}")
            service.index_videos_from_folders({"Outtrol": primary_path})
            return

        # Fallback: scan known roots for any 'outro' subfolder
        search_roots = [
            r"\\VCB_MEDIA\MEDIA VCB folder\SOURCE HUYK",
            r"\\VCB_MEDIA\MEDIA VCB folder\Generate Video",
            r"\\VCB_MEDIA\MEDIA VCB folder",
        ]
        
        target_path = None
        
        for root in search_roots:
            if not os.path.exists(root):
                continue
                
            logger.info(f"📂 Scanning '{root}' for folders containing 'outro'...")
            
            path = service.find_folder_by_name(
                root_path=root,
                target_name="outro",
                exact_match=False,
                max_depth=3
            )
            
            if path:
                target_path = path
                break
        
        if target_path:
            logger.info(f"✅ Found Outro folder: {target_path}")
            service.index_videos_from_folders({"Outtrol": target_path})
        else:
            logger.error("❌ Could not find any folder with 'outro' in name!")
            logger.error(f"💡 Searched in: {', '.join(search_roots)}")
             
    except Exception as e:
        logger.error(f"Auto-index Outro error: {e}")


