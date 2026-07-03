import os
import tempfile
import uuid
import logging
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from video_management.models import Voice
from video_management.services.minimax_voice_clone_service import get_voice_clone_service
from video_management.services.minimax_tts_service import get_minimax_service

logger = logging.getLogger(__name__)

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
            # Initialize minimax clone service
            clone_service = get_voice_clone_service()

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

        # Call Minimax service
        tts_service = get_minimax_service()
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

        return Response({
            'success': True,
            'audio_url': audio_url,
            'duration': result.get('duration', 0)
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error in Minimax TTS API: {e}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
