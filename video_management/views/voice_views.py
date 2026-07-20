import os
import re
import tempfile
import threading
import uuid
import logging
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from video_management.models import Voice
from video_management.services.minimax_voice_clone_service import get_voice_clone_service
from video_management.services.minimax_tts_service import get_minimax_service
from video_management.views.mix_progress_store import progress_set, progress_get, progress_update

logger = logging.getLogger(__name__)

# Prefix riêng cho voice-clone job trong progress store dùng chung (tên file gốc
# là "mix" nhưng cơ chế lưu trữ generic — key-value + TTL, không liên quan mix video).
_CLONE_JOB_PREFIX = "voice_clone:"

# Tên file TTS do voice_tts_api sinh: tts_<uuid4 hex>.mp3 — dùng để whitelist
# khi serve file, chặn path traversal / lấy file media tùy ý.
_TTS_FILENAME_RE = re.compile(r'^tts_[0-9a-f]{32}\.mp3$')


def _find_duplicate_cloned_voice(voice_name):
    """Tìm giọng clone trùng tên (không phân biệt hoa thường) — mỗi lần clone MiniMax
    đều tính phí và tạo voice_id mới, nên trùng tên gần như luôn là thao tác nhầm."""
    return Voice.objects.filter(name__iexact=(voice_name or '').strip(), is_cloned=True).first()


def _duplicate_voice_error(existing):
    created = existing.created_at.strftime('%d/%m/%Y') if getattr(existing, 'created_at', None) else ''
    suffix = f' (clone ngày {created})' if created else ''
    return (
        f'Đã có giọng clone tên "{existing.name}"{suffix}. '
        f'Mỗi lần clone đều tính phí MiniMax — nếu vẫn muốn tạo lại, hãy đặt tên khác hoặc xoá giọng cũ trước.'
    )


_RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')


def serve_minimax_tts_file(request, filename):
    """
    Serve file TTS đã sinh (media/minimax_tts/tts_*.mp3) — hoạt động cả khi DEBUG=False.

    Django chỉ serve /media/ qua static() khi DEBUG=True, nên trên server production
    link /media/minimax_tts/... luôn 404. BE proxy file này về trình duyệt qua
    GET api/ai/voice/tts/stream/<filename> khi chưa cấu hình Google Drive.
    Chỉ phục vụ đúng file TTS (whitelist tên tts_<hex32>.mp3), không cho lấy file khác.

    Hỗ trợ HTTP Range: FileResponse trần không set Accept-Ranges/206 — <audio> của
    trình duyệt (đặc biệt Chrome) cần Range để đọc duration của mp3 streamed, thiếu
    thì player kẹt ở 0:00/0:00 (cùng lỗi đã fix cho nhánh Drive ở BE, xem streamTtsAudio).
    """
    if not _TTS_FILENAME_RE.match(filename or ''):
        raise Http404
    path = os.path.join(default_storage.location, 'minimax_tts', filename)
    if not os.path.isfile(path):
        raise Http404

    file_size = os.path.getsize(path)
    range_match = _RANGE_RE.match(request.META.get('HTTP_RANGE', '').strip())

    if range_match:
        start_str, end_str = range_match.groups()
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        end = min(end, file_size - 1)
        if start > end or start >= file_size:
            resp = HttpResponse(status=416)
            resp['Content-Range'] = f'bytes */{file_size}'
            return resp

        length = end - start + 1

        def stream_range():
            with open(path, 'rb') as f:
                f.seek(start)
                remaining = length
                chunk_size = 65536
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        resp = StreamingHttpResponse(stream_range(), status=206, content_type='audio/mpeg')
        resp['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        resp['Content-Length'] = str(length)
        resp['Accept-Ranges'] = 'bytes'
        return resp

    resp = FileResponse(open(path, 'rb'), content_type='audio/mpeg')
    resp['Accept-Ranges'] = 'bytes'
    return resp

@api_view(['GET'])
def list_voices_api(request):
    """
    Get all available voices, including custom cloned ones and system ones.
    """
    try:
        voices = Voice.objects.all()
        voices_list = []
        for voice in voices:
            voices_list.append({
                "id": voice.id,
                "voice_id": voice.voice_id,
                "name": voice.name,
                "language": voice.language,
                "gender": voice.gender or 'female',
                "provider": voice.provider,
                "is_cloned": voice.is_cloned,
                "is_system": voice.is_system,
                "sample_audio_url": voice.sample_audio_url
            })
            
        # Fallback to default HuyK if empty
        if not voices_list:
            voices_list = [
                {
                    "id": -1,
                    "voice_id": "3f7bd9c515cb40cead3a233461c713ca",
                    "name": "HuyK",
                    "language": "vi",
                    "gender": "male",
                    "provider": "heygen",
                    "is_cloned": True,
                    "is_system": True
                }
            ]
            
        return Response({
            'success': True,
            'voices': voices_list,
            'count': len(voices_list)
        }, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"Error listing voices: {e}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def clone_voice_api(request):
    """
    Clone a voice from uploaded sample audio.
    
    POST /api/voice/clone/
    Body (multipart/form-data):
    - file: Audio file (mp3, wav, etc.)
    - voice_name: Friendly name for the voice (e.g. "Nguyen Van A")
    - gender: male or female (optional)
    """
    try:
        audio_file = request.FILES.get('file')
        voice_name = request.data.get('voice_name')
        gender = request.data.get('gender', 'female')
        
        if not audio_file:
            return Response({'error': 'file is required'}, status=status.HTTP_400_BAD_REQUEST)
        if not voice_name:
            return Response({'error': 'voice_name is required'}, status=status.HTTP_400_BAD_REQUEST)

        existing = _find_duplicate_cloned_voice(voice_name)
        if existing:
            return Response({'error': _duplicate_voice_error(existing)}, status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"🎤 Minimax Voice Cloning: name={voice_name}, file={audio_file.name}, size={audio_file.size} bytes")
        
        # Save uploaded file to a temporary location to pass to Minimax service.
        # Use a uuid-based name rather than the raw uploaded filename, so concurrent
        # uploads can't collide/overwrite each other and we don't trust user input as a path.
        temp_dir = tempfile.gettempdir()
        _, ext = os.path.splitext(audio_file.name)
        temp_path = os.path.join(temp_dir, f"voice_clone_{uuid.uuid4().hex}{ext}")

        # Write content
        with open(temp_path, 'wb') as temp_f:
            for chunk in audio_file.chunks():
                temp_f.write(chunk)

        try:
            # Key MiniMax do BE gửi kèm (X-Minimax-Key) — key lưu ở .env BE, không còn ở .env AI
            clone_service = get_voice_clone_service(api_key=request.headers.get('X-Minimax-Key'))

            # Call Minimax clone API (uploads to minimax + clones voice)
            clone_result = clone_service.clone_voice_from_file(
                audio_path=temp_path,
                voice_name=voice_name
            )
        finally:
            # Clean up temp file even if cloning fails, not just on the success path
            if os.path.exists(temp_path):
                os.remove(temp_path)

        voice_id = clone_result.get('voice_id')
        if not voice_id:
            raise Exception(f"No voice_id returned from cloning: {clone_result}")
            
        # Save/Register in Database
        voice, created = Voice.objects.update_or_create(
            voice_id=voice_id,
            defaults={
                'name': voice_name,
                'provider': 'minimax',
                'is_cloned': True,
                'is_system': False,
                'language': 'vi',
                'gender': gender
            }
        )
        
        return Response({
            'success': True,
            'message': 'Voice cloned successfully',
            'voice': {
                'id': voice.id,
                'voice_id': voice.voice_id,
                'name': voice.name,
                'provider': voice.provider,
                'gender': voice.gender
            }
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error cloning voice: {e}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def clone_voice_start_api(request):
    """
    Bắt đầu clone giọng ở chế độ NỀN và trả về job_id ngay lập tức.

    Lý do: mạng tới api.minimax.io có thể chập chờn 1-3 phút (2 IP load-balancer
    của họ thỉnh thoảng không phản hồi, xác nhận qua test thủ công 2026-07-07),
    khiến bản clone đồng bộ (clone_voice_api) dễ bị BE/FE tự timeout giữa chừng
    dù MiniMax cuối cùng vẫn xử lý xong. Client poll qua
    GET /api/voice/clone/status/<job_id>/ thay vì chờ 1 request treo.

    POST /api/voice/clone/start/
    Body (multipart/form-data): file, voice_name, gender (optional)
    Response: { success, job_id }
    """
    try:
        audio_file = request.FILES.get('file')
        voice_name = request.data.get('voice_name')
        gender = request.data.get('gender', 'female')

        if not audio_file:
            return Response({'error': 'file is required'}, status=status.HTTP_400_BAD_REQUEST)
        if not voice_name:
            return Response({'error': 'voice_name is required'}, status=status.HTTP_400_BAD_REQUEST)

        existing = _find_duplicate_cloned_voice(voice_name)
        if existing:
            return Response({'error': _duplicate_voice_error(existing)}, status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"🎤 Minimax Voice Cloning (bg): name={voice_name}, file={audio_file.name}, size={audio_file.size} bytes")

        temp_dir = tempfile.gettempdir()
        _, ext = os.path.splitext(audio_file.name)
        temp_path = os.path.join(temp_dir, f"voice_clone_{uuid.uuid4().hex}{ext}")
        with open(temp_path, 'wb') as temp_f:
            for chunk in audio_file.chunks():
                temp_f.write(chunk)

        # Bắt key ra biến TRƯỚC khi spawn thread — request object không dùng được an toàn trong thread nền
        minimax_key = request.headers.get('X-Minimax-Key')

        job_id = uuid.uuid4().hex
        job_key = f"{_CLONE_JOB_PREFIX}{job_id}"
        progress_set(job_key, {
            'status': 'queued',
            'message': 'Đang chờ xử lý...',
            'voice_name': voice_name,
        })

        def _run_clone_job():
            progress_update(job_key, {'status': 'running', 'message': 'Đang upload + clone giọng (có thể mất vài phút nếu mạng chập chờn)...'})
            try:
                clone_service = get_voice_clone_service(api_key=minimax_key)
                clone_result = clone_service.clone_voice_from_file(audio_path=temp_path, voice_name=voice_name)

                voice_id = clone_result.get('voice_id')
                if not voice_id:
                    raise Exception(f"No voice_id returned from cloning: {clone_result}")

                voice, _created = Voice.objects.update_or_create(
                    voice_id=voice_id,
                    defaults={
                        'name': voice_name,
                        'provider': 'minimax',
                        'is_cloned': True,
                        'is_system': False,
                        'language': 'vi',
                        'gender': gender,
                    }
                )
                progress_update(job_key, {
                    'status': 'completed',
                    'message': 'Voice cloned successfully',
                    'voice': {
                        'id': voice.id,
                        'voice_id': voice.voice_id,
                        'name': voice.name,
                        'provider': voice.provider,
                        'gender': voice.gender,
                    },
                })
            except Exception as e:
                logger.error(f"[Voice Clone bg] job {job_id} failed: {e}", exc_info=True)
                progress_update(job_key, {'status': 'error', 'message': str(e)})
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        threading.Thread(target=_run_clone_job, name=f"voice_clone_{job_id}", daemon=True).start()

        return Response({'success': True, 'job_id': job_id}, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Error starting voice clone job: {e}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
def clone_voice_job_status_api(request, job_id):
    """
    GET /api/voice/clone/status/<job_id>/
    Response: { success, status: queued|running|completed|error, message, voice? }
    """
    data = progress_get(f"{_CLONE_JOB_PREFIX}{job_id}")
    if data is None:
        return Response({'error': 'job not found'}, status=status.HTTP_404_NOT_FOUND)
    return Response({'success': True, **data}, status=status.HTTP_200_OK)


@api_view(['POST'])
def voice_tts_api(request):
    """
    Generate Text-to-Speech audio using Minimax.
    
    POST /api/voice/tts/
    Body:
    {
        "text": "Văn bản cần đọc...",
        "voice_id": "minimax_voice_id",
        "speed": 1.0,         // speed multiplier (0.5 - 2.0)
        "pitch": 0,           // pitch adjustment (-12 to 12)
        "volume": 100         // volume level (0 - 100)
    }
    """
    try:
        text = request.data.get('text')
        voice_id = request.data.get('voice_id')
        speed = float(request.data.get('speed', 1.0))
        pitch = int(request.data.get('pitch', 0))
        volume = int(request.data.get('volume', 100))
        language = request.data.get('language') or None

        if not text:
            return Response({'error': 'text is required'}, status=status.HTTP_400_BAD_REQUEST)
        if not voice_id:
            return Response({'error': 'voice_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"🎤 Minimax TTS Request: voice={voice_id}, text_len={len(text)}, speed={speed}, pitch={pitch}, vol={volume}, language={language}")

        # Check the voice belongs to Minimax before forwarding — a HeyGen/ElevenLabs
        # voice_id would otherwise be sent straight to Minimax and fail with an opaque error.
        voice = Voice.objects.filter(voice_id=voice_id).first()
        if voice and voice.provider and voice.provider != 'minimax':
            return Response(
                {'error': f"Voice '{voice_id}' belongs to provider '{voice.provider}', not Minimax"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Adjust volume from 0-100 scale to Minimax 0.1-10.0 scale
        vol_minimax = max(0.1, min(10.0, volume / 100.0 * 1.0))

        # Prepare output audio file path in Django's media storage
        media_root = default_storage.location
        audio_dir = os.path.join(media_root, 'minimax_tts')
        if not os.path.exists(audio_dir):
            os.makedirs(audio_dir, exist_ok=True)

        filename = f"tts_{uuid.uuid4().hex}.mp3"
        output_path = os.path.join(audio_dir, filename)

        # Key MiniMax do BE gửi kèm (X-Minimax-Key) — key lưu ở .env BE, không còn ở .env AI
        tts_service = get_minimax_service(api_key=request.headers.get('X-Minimax-Key'))
        result = tts_service.generate_audio(
            text=text,
            voice_id=voice_id,
            speed=speed,
            vol=vol_minimax,
            pitch=pitch,
            language_boost=language,
            output_path=output_path
        )
        
        # Generate the public URL to serve this media file. Nếu vì lý do nào đó
        # file không được ghi ra output_path (Minimax trả URL mà download lỗi),
        # trả thẳng URL của Minimax thay vì một đường dẫn /media 404.
        ai_url = os.getenv('AI_SERVICE_URL', 'http://localhost:8001')
        if os.path.exists(output_path):
            audio_url = f"{ai_url}/media/minimax_tts/{filename}"
        else:
            audio_url = result.get('audio_url')
            if not audio_url or not str(audio_url).startswith('http'):
                raise Exception('TTS succeeded but no playable audio file/URL was produced')

        extra_info = result.get('extra_info') or {}

        # Trả kèm bytes base64 khi file thực sự nằm trên đĩa của AI — BE dùng để
        # upload thẳng lên Drive (hoặc nhúng data: URL) thay vì GET ngược lại route
        # /api/voice/tts/file/<filename> bên dưới. Route đó chỉ đọc được file nếu
        # request rơi đúng vào instance/đĩa vừa ghi — trên Railway (nhiều
        # replica/không có volume dùng chung) GET-lại gần như luôn 404 ngay cả khi
        # gọi lại tức thì, làm hỏng cả nhánh upload Drive lẫn nhánh phát/tải của FE.
        audio_base64 = None
        if os.path.exists(output_path):
            with open(output_path, 'rb') as f:
                import base64
                audio_base64 = base64.b64encode(f.read()).decode('ascii')

        return Response({
            'success': True,
            'audio_url': audio_url,
            'audio_base64': audio_base64,
            'duration': result.get('duration', 0),
            # Số ký tự MiniMax thực tính phí (khớp đơn vị "điểm âm thanh" của gói) —
            # BE dùng để ghi log tiêu dùng theo user.
            'usage_characters': extra_info.get('usage_characters', 0),
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error in Minimax TTS API: {e}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
