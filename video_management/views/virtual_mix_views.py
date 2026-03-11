"""
Virtual Mix Views - Smart video preview.

Strategy:
  1. Try cached clips first (instant)
  2. If not enough → show all slots, use stream URL for uncached
  3. Show progress to user in real-time via polling
"""

import os
import json
import random
import logging
import mimetypes
import threading
import time as _time
from typing import Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.http import StreamingHttpResponse, FileResponse
from django.conf import settings
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from video_management.models import IndexedVideo, VideoClipCache
from video_management.services.smart_preprocessing_service import (
    get_preprocessing_service,
)

logger = logging.getLogger(__name__)

# Progress tracking for async generation
_gen_progress = {}
_gen_lock = threading.Lock()


@api_view(['POST'])
def virtual_mix(request):
    """
    Create virtual mix manifests.

    Smart strategy:
    - Build dynamic slots (3-4s each) using _build_dynamic_formula
    - Pick videos for each slot
    - Use cached clips where available (instant stream)
    - Fall back to stream/video_id for uncached clips
    - Returns manifest with ALL slots visible
    """
    start_time = _time.time()

    try:
        audio_file = request.FILES.get('audio')
        num_outputs = int(request.POST.get('num_outputs', 5))
        product_sku = request.POST.get('product_sku')
        use_a4 = request.POST.get('use_a4_formula', 'true').lower() == 'true'

        if not audio_file:
            return Response({'error': 'Audio file is required'}, status=400)

        # Save audio
        audio_dir = os.path.join(settings.MEDIA_ROOT, 'virtual_mix_audio')
        os.makedirs(audio_dir, exist_ok=True)
        audio_id = f"{random.randint(100000, 999999)}"
        audio_path = os.path.join(audio_dir, f'audio_{audio_id}.mp3')

        with open(audio_path, 'wb') as f:
            for chunk in audio_file.chunks():
                f.write(chunk)

        service = get_preprocessing_service()
        audio_duration = _get_audio_duration(service.ffprobe_path, audio_path)

        if not audio_duration or audio_duration <= 0:
            return Response({'error': 'Could not determine audio duration'}, status=400)

        # ── Build slot configs dùng Dynamic Formula V4 ─────────────
        from video_management.views.smart_mix_video_views import _build_dynamic_formula

        if use_a4:
            # Mỗi slot 3-4s, tự động tính số slot dựa trên audio_duration
            dynamic_slots = _build_dynamic_formula(audio_duration)
            slot_configs = [
                {
                    'name': s['folder_type'],
                    'folder_type': s['folder_type'],
                    'duration': s['duration']
                }
                for s in dynamic_slots
            ]
            # Thêm Outro cuối
            slot_configs.append({
                'name': 'Outtrol',
                'folder_type': 'Outtrol',
                'duration': None  # dùng duration gốc của video
            })
            logger.info(
                f"🎬 Virtual Mix V4: {len(slot_configs)} slots "
                f"({len(dynamic_slots)} content + 1 outro) for {audio_duration:.1f}s"
            )
        else:
            slot_duration_r = audio_duration / 5
            slot_configs = [
                {'name': f'Clip {i+1}', 'folder_type': 'Sản phẩm', 'duration': slot_duration_r}
                for i in range(5)
            ]

        # ── Pre-load cache map (instant DB query) ──────────────────
        cache_map = {}  # video_id → cache_info
        for cache in VideoClipCache.objects.all():
            if os.path.isfile(cache.clip_path):
                fsize = os.path.getsize(cache.clip_path)
                if fsize < 100_000:  # Skip corrupted clips (< 100KB)
                    logger.warning(f"⚠️ Clip {cache.id} too small ({fsize} bytes), skipping")
                    continue
                cache_map[cache.source_video_id] = {
                    'cache_id': cache.id,
                    'duration': cache.duration or 12.0,
                }

        logger.info(f"⚡ Virtual Mix: audio={audio_duration:.1f}s, cached={len(cache_map)} clips")

        # ── Pre-check: tất cả slot types phải có ít nhất 1 cached clip ──
        # Nếu thiếu bất kỳ loại nào → block preview hoàn toàn
        required_folder_types = list(dict.fromkeys(c['folder_type'] for c in slot_configs))
        missing_types = []
        for ft in required_folder_types:
            qs = IndexedVideo.objects.filter(folder_type=ft, is_available=True)
            all_ids = list(qs.values_list('id', flat=True))
            has_cache = any(vid in cache_map for vid in all_ids)
            if not has_cache:
                missing_types.append(ft)

        if missing_types:
            missing_str = ', '.join(missing_types)
            logger.warning(f"🚫 Preview blocked: missing cached clips for: {missing_str}")
            return Response({
                'error': 'preview_not_ready',
                'message': f'Preview chưa sẵn sàng. Các loại clip sau chưa được cache: {missing_str}. Vui lòng chạy Pre-generation trước.',
                'missing_types': missing_types,
            }, status=400)

        # ── Select videos cho preview – CHỈ dùng cached clips ────
        all_selections = []

        for output_idx in range(num_outputs):
            used_ids = set()
            for slot_idx, config in enumerate(slot_configs):
                ft = config['folder_type']

                # Get available videos
                qs = IndexedVideo.objects.filter(
                    folder_type=ft, is_available=True
                )
                if product_sku and ft in ['Sản phẩm', 'Sản phẩm HT']:
                    sku_filtered = qs.filter(file_path__icontains=product_sku)
                    if sku_filtered.exists():
                        qs = sku_filtered

                candidates = list(qs.exclude(id__in=used_ids).values_list('id', flat=True))
                if not candidates:
                    candidates = list(qs.values_list('id', flat=True))
                if not candidates:
                    logger.warning(f"⚠️ No videos for slot {slot_idx+1} ({ft}), skipping")
                    continue

                # CHỈ chọn video đã có cached clips
                cached_candidates = [v for v in candidates if v in cache_map]
                if not cached_candidates:
                    logger.warning(f"⚠️ Slot {slot_idx+1} ({ft}): no cached clips")
                    continue

                vid = random.choice(cached_candidates)
                used_ids.add(vid)
                all_selections.append((output_idx, slot_idx, vid, config))

        logger.info(f"⚡ Virtual Mix: {len(all_selections)} slots with cached clips selected")

        # ── Build manifests ────────────────────────────────────────
        manifests = []
        for output_idx in range(num_outputs):
            clips = []
            for (oi, si, vid, config) in all_selections:
                if oi != output_idx:
                    continue

                cache_info = cache_map.get(vid)

                dur = config['duration']
                if dur is None:
                    dur = cache_info['duration'] if cache_info else 12.0

                # All selected videos guaranteed to have cache (selected above)
                stream_url = f'/api/videos/stream-clip/{cache_info["cache_id"]}/'

                clips.append({
                    'video_id': vid,
                    'slot': si + 1,
                    'slot_name': config['name'],
                    'duration': round(dur, 2),
                    'stream_url': stream_url,
                    'folder_type': config['folder_type'],
                    'cached': True,
                })

            if clips:
                manifests.append({
                    'output_index': output_idx,
                    'clips': clips,
                    'audio_url': f'/api/videos/stream-audio/{audio_id}/',
                    'total_duration': round(audio_duration, 2),
                    'slot_count': len(clips),
                })

        generation_time = (_time.time() - start_time) * 1000

        logger.info(
            f"✅ Virtual Mix: {len(manifests)} manifests, "
            f"{len(all_selections)} cached clips, "
            f"{generation_time:.0f}ms"
        )

        return Response({
            'success': True,
            'manifests': manifests,
            'audio_duration': audio_duration,
            'audio_id': audio_id,
            'generation_time_ms': round(generation_time),
            'clips_generated': 0,
            'clips_from_cache': len(all_selections),
            'message': f'Created {len(manifests)} mixes in {generation_time/1000:.1f}s'
        })

    except Exception as e:
        logger.error(f"Virtual mix error: {e}", exc_info=True)
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def stream_video(request, video_id):
    """Stream video's clip, generating it seamlessly if not yet cached."""
    try:
        service = get_preprocessing_service()
        # This checks cache first, then generates if missing (auto-detects GPU)
        clip_path = service.get_or_generate_clip(video_id, use_gpu=None)

        if clip_path and os.path.isfile(clip_path):
            return _serve_file_with_range(request, clip_path)

        return Response({'error': 'Failed to generate preview clip'}, status=404)
    except Exception as e:
        logger.error(f"stream_video on-the-fly generation error: {e}")
        return Response({'error': str(e)}, status=404)


@api_view(['GET'])
def stream_clip(request, clip_id):
    """Stream a cached clip file."""
    try:
        clip = VideoClipCache.objects.get(id=clip_id)
        if not os.path.isfile(clip.clip_path):
            return Response({'error': 'Clip file not found'}, status=404)
        return _serve_file_with_range(request, clip.clip_path)
    except VideoClipCache.DoesNotExist:
        return Response({'error': 'Clip not found'}, status=404)


@api_view(['GET'])
def stream_audio(request, audio_id):
    """Stream uploaded audio file."""
    audio_path = os.path.join(
        settings.MEDIA_ROOT, 'virtual_mix_audio', f'audio_{audio_id}.mp3'
    )
    if not os.path.isfile(audio_path):
        return Response({'error': 'Audio not found'}, status=404)
    return _serve_file_with_range(request, audio_path)


def _serve_file_with_range(request, file_path):
    """Serve file with HTTP Range support."""
    file_size = os.path.getsize(file_path)
    content_type = mimetypes.guess_type(file_path)[0] or 'video/mp4'

    range_header = request.META.get('HTTP_RANGE', '')

    if range_header:
        try:
            range_spec = range_header.replace('bytes=', '')
            parts = range_spec.split('-')
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            def file_iterator():
                with open(file_path, 'rb') as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        data = f.read(min(65536, remaining))
                        if not data:
                            break
                        remaining -= len(data)
                        yield data

            response = StreamingHttpResponse(
                file_iterator(), status=206, content_type=content_type
            )
            response['Content-Length'] = length
            response['Content-Range'] = f'bytes {start}-{end}/{file_size}'
            response['Accept-Ranges'] = 'bytes'
            response['Access-Control-Allow-Origin'] = '*'
            return response
        except (ValueError, IndexError):
            pass

    response = FileResponse(open(file_path, 'rb'), content_type=content_type)
    response['Content-Length'] = file_size
    response['Accept-Ranges'] = 'bytes'
    response['Access-Control-Allow-Origin'] = '*'
    return response


def _get_audio_duration(ffprobe_path, audio_path):
    """Get audio duration using ffprobe."""
    import subprocess
    try:
        result = subprocess.run(
            [ffprobe_path, '-v', 'quiet', '-show_entries',
             'format=duration', '-of', 'json', audio_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return float(data['format']['duration'])
    except Exception as e:
        logger.error(f"ffprobe error: {e}")
    return None


@api_view(['POST'])
def virtual_mix_render(request):
    """Placeholder for full render."""
    return Response({
        'success': True,
        'message': 'Use /api/videos/smart-mix/ for full render.',
    })
