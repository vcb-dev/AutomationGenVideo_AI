"""
Virtual Mix Views - Smart video preview.

Strategy:
  1. Try cached clips first (instant)
  2. If not enough → generate ONLY the clips needed (7 clips, not 112)
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
    - Pick videos for each slot
    - Use cached clips where available
    - Generate missing clips on-the-fly (only what's needed, ~7 clips)
    - Returns manifest with stream URLs
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
        
        # ── Build slot configs ─────────────────────────────────────
        slot_duration = audio_duration / 6 if use_a4 else audio_duration / 5
        
        if use_a4:
            slot_configs = [
                {'name': 'Sản phẩm', 'folder_type': 'Sản phẩm', 'duration': slot_duration},
                {'name': 'HuyK', 'folder_type': 'HuyK', 'duration': slot_duration},
                {'name': 'Chế tác', 'folder_type': 'Chế tác', 'duration': slot_duration},
                {'name': 'HuyK', 'folder_type': 'HuyK', 'duration': slot_duration},
                {'name': 'Chế tác', 'folder_type': 'Chế tác', 'duration': slot_duration},
                {'name': 'Sản phẩm HT', 'folder_type': 'Sản phẩm HT', 'duration': slot_duration},
                {'name': 'Outtrol', 'folder_type': 'Outtrol', 'duration': None},
            ]
        else:
            slot_configs = [
                {'name': f'Clip {i+1}', 'folder_type': 'Sản phẩm', 'duration': slot_duration}
                for i in range(5)
            ]
        
        # ── Pre-load cache map (instant DB query) ──────────────────
        cache_map = {}  # video_id → cache_id
        for cache in VideoClipCache.objects.all():
            if os.path.isfile(cache.clip_path):
                cache_map[cache.source_video_id] = {
                    'cache_id': cache.id,
                    'duration': cache.duration or 12.0,
                }
        
        logger.info(f"⚡ Virtual Mix: audio={audio_duration:.1f}s, cached={len(cache_map)} clips")
        
        # ── Select videos for ALL outputs, prefer cached ──────────
        all_selections = []  # [(output_idx, slot_idx, video_id, config)]
        need_gen = set()  # video IDs that need clip generation
        
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
                    continue
                
                # Prefer videos that already have cache
                cached_candidates = [v for v in candidates if v in cache_map]
                
                if cached_candidates:
                    vid = random.choice(cached_candidates)
                else:
                    vid = random.choice(candidates)
                    need_gen.add(vid)
                
                used_ids.add(vid)
                all_selections.append((output_idx, slot_idx, vid, config))
        
        # ── Generate missing clips (only what's needed!) ───────────
        if need_gen:
            logger.info(f"📹 Need to generate {len(need_gen)} clips (others from cache)")
            
            for vid_id in need_gen:
                try:
                    logger.info(f"  🔧 Generating clip for video {vid_id}...")
                    clip_path = service.get_or_generate_clip(vid_id, use_gpu=None)
                    if clip_path:
                        # Refresh cache map
                        cached = VideoClipCache.objects.filter(source_video_id=vid_id).first()
                        if cached:
                            cache_map[vid_id] = {
                                'cache_id': cached.id,
                                'duration': cached.duration or 12.0,
                            }
                            logger.info(f"  ✅ Clip ready for video {vid_id}")
                    else:
                        logger.warning(f"  ❌ Failed to generate clip for video {vid_id}")
                except Exception as e:
                    logger.error(f"  ❌ Clip gen error for {vid_id}: {e}")
        
        # ── Build manifests ────────────────────────────────────────
        manifests = []
        for output_idx in range(num_outputs):
            clips = []
            for (oi, si, vid, config) in all_selections:
                if oi != output_idx:
                    continue
                
                cache_info = cache_map.get(vid)
                if not cache_info:
                    continue  # Skip if generation failed
                
                dur = config['duration']
                if dur is None:
                    dur = cache_info['duration']
                
                clips.append({
                    'video_id': vid,
                    'slot': si + 1,
                    'slot_name': config['name'],
                    'duration': round(dur, 2),
                    'stream_url': f'/api/videos/stream-clip/{cache_info["cache_id"]}/',
                    'folder_type': config['folder_type'],
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
            f"{len(need_gen)} generated + {len(all_selections) - len(need_gen)} from cache, "
            f"{generation_time:.0f}ms"
        )
        
        return Response({
            'success': True,
            'manifests': manifests,
            'audio_duration': audio_duration,
            'audio_id': audio_id,
            'generation_time_ms': round(generation_time),
            'clips_generated': len(need_gen),
            'clips_from_cache': len(all_selections) - len(need_gen),
            'message': f'Created {len(manifests)} mixes in {generation_time/1000:.1f}s'
        })
    
    except Exception as e:
        logger.error(f"Virtual mix error: {e}", exc_info=True)
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def stream_video(request, video_id):
    """Stream video's cached clip."""
    try:
        cached = VideoClipCache.objects.filter(source_video_id=video_id).first()
        if cached and os.path.isfile(cached.clip_path):
            return _serve_file_with_range(request, cached.clip_path)
        return Response({'error': 'No cached clip'}, status=404)
    except Exception:
        return Response({'error': 'Not found'}, status=404)


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
