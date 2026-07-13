"""
HeyGen Video Generation API Views
Django REST Framework endpoints for lipsync/motion control
"""

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import json
import asyncio
import logging
from .heygen_client import HeyGenClient, generate_video_from_text


from .models import VideoGenerationRequest, VoiceSettings, VideoAspectRatio, VideoQuality
from video_management.models import Voice
from video_management.services.voice_extraction_service import VoiceExtractionService
from openai import OpenAI
# from video_management.services.elevenlabs_service import ElevenLabsService
from .ai_writer import AIWriter
from django.conf import settings
import os

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["POST"])
def generate_script(request):
    """
    Generate video script using AI (OpenAI)
    
    POST /api/heygen/generate-script
    
    Request Body:
    {
        "topic": "Giới thiệu nhẫn kim cương",
        "tone": "professional" // "professional", "casual", "sale"
    }
    
    Response:
    {
        "success": true,
        "data": {
            "script": "Xin chào..."
        }
    }
    """
    try:
        data = json.loads(request.body)
        topic = data.get('topic')
        tone = data.get('tone', 'professional')

        if not topic:
             return JsonResponse({
                'success': False,
                'error': 'Topic is required'
            }, status=400)

        ai_writer = AIWriter()
        script = ai_writer.generate_script(topic, tone)

        return JsonResponse({
            'success': True,
            'data': {
                'script': script
            }
        })
    except Exception as e:
        logger.error(f"Generate script error: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def generate_video(request):
    """
    Generate video with HeyGen avatar lipsync
    
    POST /api/heygen/generate-video
    
    Request Body:
    {
        "text": "Xin chào! Tôi là đại diện thương hiệu Viễn Chí Bảo.",
        "voice_id": "vi-VN-female-1",  // Optional
        "aspect_ratio": "16:9",  // "16:9", "9:16", "1:1"
        "quality": "high",  // "low", "medium", "high", "ultra"
        "background_color": "#FFFFFF",  // Optional
        "title": "Brand Video"  // Optional
    }
    
    Response:
    {
        "success": true,
        "data": {
            "video_id": "vid_xyz789",
            "status": "pending",
            "message": "Video generation started"
        }
    }
    """
    try:
        # Parse request body
        data = json.loads(request.body)
        
        text = data.get('text')
        if not text:
            return JsonResponse({
                'success': False,
                'error': 'Text is required'
            }, status=400)
        
        # Extract parameters
        voice_id = data.get('voice_id')
        avatar_id = data.get('avatar_id')
        
        # HOTFIX: If old Talking Photo ID is sent (due to frontend cache), swap it with the new Video Avatar ID
        if avatar_id == 'c73ebf9cd4ea4d0e9764ecf684999d71' or avatar_id == '871cc4b87b8643c6b9cc6b4cf6797fc9':
            avatar_id = '4a4cbf45415048f79c6c7968e353a359'

        # Sanitize avatar_style
        raw_style = data.get('avatar_style', 'normal')
        valid_styles = ['normal', 'closeUp', 'full', 'circle', 'voiceOnly']
        avatar_style = raw_style if raw_style in valid_styles else 'normal'
        if raw_style not in valid_styles:
            logger.warning(f"Invalid avatar_style '{raw_style}' received. Defaulting to 'normal'.")

        aspect_ratio = data.get('aspect_ratio', '16:9')
        quality = data.get('quality', 'medium')
        background_color = data.get('background_color', '#FFFFFF')
        title = data.get('title')
        
        # Validate aspect ratio
        if aspect_ratio not in ['16:9', '9:16', '1:1']:
            return JsonResponse({
                'success': False,
                'error': 'Invalid aspect_ratio. Must be 16:9, 9:16, or 1:1'
            }, status=400)
        

        # Check if voice is from ElevenLabs (or custom cloned)
        voice = None
        voice_settings = None
        
        # Try to find voice in DB
        if voice_id:
            try:
                voice = Voice.objects.filter(voice_id=voice_id).first()
            except Exception:
                pass
        
        # If it's a cloned/ElevenLabs voice, we OLDLY generated audio first.
        # NOW with HeyGen cloning, we can use the voice_id directly with HeyGen API.
        if voice and voice.provider == 'elevenlabs':
             # Legacy support or if we still have old data. 
             # If we want to fully switch, we might want to warn or just let it fail if key is gone.
             # But better to just log and try standard flow (which will fail if HeyGen doesn't know this ID)
             # or just remove this block.
             # User asked to remove ElevenLabs logic.
             pass
        
        # NOTE: If voice.provider == 'heygen' (our new logic), it falls through to standard flow below
        # which sends 'voice_id' to HeyGen. This is correct.


        # Create video generation request
        logger.info(f"Generating video with text: {text[:50]}...")
        
        # Run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # If we created custom voice settings (audio), we need to pass them constructed
            # BUT the helper 'generate_video_from_text' expects text+voice_id.
            # We need to call client.create_video directly if using audio.
            
            client = HeyGenClient()
            
            if voice_settings and voice_settings.type == 'audio':
                 req = VideoGenerationRequest(
                    avatar_id=avatar_id,
                    avatar_style=avatar_style,
                    voice_settings=voice_settings,
                    aspect_ratio=aspect_ratio,
                    quality=quality,
                    background_color=background_color,
                    title=title or text[:20]
                )
                 response = loop.run_until_complete(client.create_video(req))
            else:
                # Standard Text flow
                response = loop.run_until_complete(
                    generate_video_from_text(
                        text=text,
                        voice_id=voice_id,
                        avatar_id=avatar_id,
                        avatar_style=avatar_style,
                        aspect_ratio=aspect_ratio,
                        wait_for_completion=False 
                    )
                )
            
            return JsonResponse({
                'success': True,
                'data': {
                    'video_id': response.video_id,
                    'status': response.status,
                    'message': 'Video generation started use /status endpoint to check progress.'
                }
            })
            
        finally:
            loop.close()
        
    except Exception as e:
        logger.error(f"Error generating video: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def get_video_status(request, video_id):
    """
    Get video generation status
    
    GET /api/heygen/status/<video_id>
    
    Response:
    {
        "success": true,
        "data": {
            "video_id": "vid_xyz789",
            "status": "completed",  // "pending", "processing", "completed", "failed"
            "progress": 100,
            "video_url": "https://...",
            "thumbnail_url": "https://...",
            "duration": 45.5
        }
    }
    """
    try:
        client = HeyGenClient()
        
        # Run async function
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            status = loop.run_until_complete(
                client.get_video_status(video_id)
            )
            
            return JsonResponse({
                'success': True,
                'data': {
                    'video_id': status.video_id,
                    'status': status.status,
                    'progress': status.progress,
                    'video_url': status.video_url,
                    'thumbnail_url': status.thumbnail_url,
                    'duration': status.duration,
                    'error': status.error,
                    'estimated_time': status.estimated_time
                }
            })
            
        finally:
            loop.close()
        
    except Exception as e:
        logger.error(f"Error getting video status: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def generate_and_wait(request):
    """
    Generate video and wait for completion (synchronous)
    
    POST /api/heygen/generate-and-wait
    
    Request Body: Same as /generate-video
    
    Response:
    {
        "success": true,
        "data": {
            "video_id": "vid_xyz789",
            "status": "completed",
            "video_url": "https://...",
            "thumbnail_url": "https://...",
            "duration": 45.5
        }
    }
    
    Note: This endpoint may take 2-5 minutes to respond
    """
    try:
        # Parse request body
        data = json.loads(request.body)
        
        text = data.get('text')
        if not text:
            return JsonResponse({
                'success': False,
                'error': 'Text is required'
            }, status=400)
        
        voice_id = data.get('voice_id')
        
        # Sanitize avatar_style
        raw_style = data.get('avatar_style', 'normal')
        valid_styles = ['normal', 'closeUp', 'full', 'circle', 'voiceOnly']
        avatar_style = raw_style if raw_style in valid_styles else 'normal'
        
        aspect_ratio = data.get('aspect_ratio', '16:9')
        
        logger.info(f"Generating video (sync) with text: {text[:50]}...")
        
        # Run async function and WAIT for completion
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            response = loop.run_until_complete(
                generate_video_from_text(
                    text=text,
                    voice_id=voice_id,
                    avatar_style=avatar_style,
                    aspect_ratio=aspect_ratio,
                    wait_for_completion=True  # Wait for video to complete
                )
            )
            
            return JsonResponse({
                'success': True,
                'data': {
                    'video_id': response.video_id,
                    'status': response.status,
                    'video_url': response.video_url,
                    'thumbnail_url': response.thumbnail_url,
                    'duration': response.duration
                }
            })
            
        finally:
            loop.close()
        
    except Exception as e:
        logger.error(f"Error generating video (sync): {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def list_voices(request):
    """
    Get available voice options
    
    GET /api/heygen/voices
    
    Response:
    {
        "success": true,
        "data": {
            "voices": [...]
        }
    }
    """
    # Hardcoded system voices
    system_voices = [
        {
            "voice_id": "c6fb81520dcd42e0a02be231046a8639",
            "name": "Nam Minh (Natural)",
            "language": "vi-VN",
            "gender": "male",
            "provider": "heygen",
            "is_cloned": False
        },
        {
            "voice_id": "4286c03d11f44af093e379fc7e2cafa6",
            "name": "Chau (Natural)",
            "language": "vi-VN",
            "gender": "female",
            "provider": "heygen",
            "is_cloned": False
        },
        {
            "voice_id": "9a247a37f3c04e6aa934171998b9659c",
            "name": "Hoai (Natural)",
            "language": "vi-VN",
            "gender": "female",
            "provider": "heygen",
            "is_cloned": False
        },
        {
            "voice_id": "3f7bd9c515cb40cead3a233461c713ca",
            "name": "HuyK (TikTok)",
            "language": "vi-VN",
            "gender": "male",
            "provider": "heygen",
            "is_cloned": True
        },
        {
            "voice_id": "QJxxo4VGVj80QFWwztX8",
            "name": "huyk2.MP3",
            "language": "vi-VN",
            "gender": "male",
            "provider": "heygen",
            "is_cloned": True
        }
    ]

    # OpenAI Voices
    openai_voices = [
        {"voice_id": "alloy", "name": "Alloy (OpenAI - Neutral)", "language": "mul", "gender": "neutral", "provider": "openai", "is_cloned": False},
        {"voice_id": "echo", "name": "Echo (OpenAI - Male)", "language": "mul", "gender": "male", "provider": "openai", "is_cloned": False},
        {"voice_id": "fable", "name": "Fable (OpenAI - British)", "language": "mul", "gender": "male", "provider": "openai", "is_cloned": False},
        {"voice_id": "onyx", "name": "Onyx (OpenAI - Male)", "language": "mul", "gender": "male", "provider": "openai", "is_cloned": False},
        {"voice_id": "nova", "name": "Nova (OpenAI - Female)", "language": "mul", "gender": "female", "provider": "openai", "is_cloned": False},
        {"voice_id": "shimmer", "name": "Shimmer (OpenAI - Female)", "language": "mul", "gender": "female", "provider": "openai", "is_cloned": False},
    ]
    
    # Get cloned voices from DB
    try:
        db_voices = Voice.objects.all().values(
            'voice_id', 'name', 'provider', 'is_cloned', 'language', 'gender'
        )
        cloned_voices = list(db_voices)
    except Exception as e:
        logger.error(f"Error fetching DB voices: {str(e)}")
        cloned_voices = []
    
    # Merge lists
    # all_voices = system_voices + openai_voices + cloned_voices 
    # User requested HeyGen voices only for now (or at least prioritize them)
    all_voices = system_voices + openai_voices + cloned_voices
    
    return JsonResponse({
        'success': True,
        'data': {
            'voices': all_voices
        }
    })


@csrf_exempt
@require_http_methods(["POST"])
def clone_voice_from_video(request):
    """
    Clone voice from a KOC video URL
    
    POST /api/heygen/clone-voice
    
    Request Body:
    {
        "video_url": "https://tiktok.com/...",
        "voice_name": "My KOC Voice"
    }
    """
    try:
        data = json.loads(request.body)
        video_url = data.get('video_url')
        voice_name = data.get('voice_name')
        
        if not video_url or not voice_name:
            return JsonResponse({
                'success': False, 
                'error': 'video_url and voice_name are required'
            }, status=400)
            
        extractor = VoiceExtractionService()
        result = extractor.process_koc_voice_and_clone(video_url, voice_name, is_url=True)
        
        if result.get('success'):
            # Save to Database
            Voice.objects.create(
                name=voice_name,
                voice_id=result['voice_id'],
                provider=result['provider'], # 'elevenlabs'
                is_cloned=True,
                language='vi', # Default to Vietnamese for now
                sample_audio_url=video_url # Store source URL as reference
            )
            
            return JsonResponse({
                'success': True,
                'data': result
            })
        else:
            return JsonResponse({
                'success': False,
                'error': result.get('error', 'Unknown cloning error')
            }, status=500)
            
    except Exception as e:
        logger.error(f"Clone Voice API Error: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@csrf_exempt
@require_http_methods(["POST"])
@csrf_exempt
@require_http_methods(["POST"])
def generate_audio(request):
    """
    Generate audio (TTS) for preview.
    Supports OpenAI and HeyGen (via video extraction).
    """
    try:
        data = json.loads(request.body)
        text = data.get('text')
        voice_id = data.get('voice_id')
        
        if not text:
             return JsonResponse({'success': False, 'error': 'Text is required'}, status=400)
        
        import uuid
        openai_voice_ids = ['alloy', 'echo', 'fable', 'onyx', 'nova', 'shimmer']

        # 1. OpenAI Strategy
        if voice_id in openai_voice_ids:
            try:
                client = OpenAI(api_key=settings.OPENAI_API_KEY)
                filename = f"openai_tts_{uuid.uuid4()}.mp3"
                output_dir = os.path.join(settings.MEDIA_ROOT, "generated_audio")
                os.makedirs(output_dir, exist_ok=True)
                output_path = os.path.join(output_dir, filename)
                
                response = client.audio.speech.create(
                    model="tts-1",
                    voice=voice_id,
                    input=text
                )
                response.stream_to_file(output_path)
                
                relative_path = os.path.relpath(output_path, settings.MEDIA_ROOT)
                audio_url = settings.MEDIA_URL + relative_path.replace('\\', '/')
                return JsonResponse({'success': True, 'data': {'audio_url': audio_url}})
            except Exception as e:
                 logger.error(f"OpenAI TTS Error: {str(e)}")
                 return JsonResponse({'success': False, 'error': f"Lỗi tạo audio OpenAI: {str(e)}"}, status=500)

        # 2. HeyGen / Non-OpenAI voices → Use OpenAI TTS as fallback
        #    (HeyGen does NOT have a standalone TTS API — it requires full video generation
        #     which is slow, expensive, and unreliable. Use OpenAI TTS instead for speed.)
        else:
            try:
                logger.info(f"Voice {voice_id} is not OpenAI — using OpenAI TTS fallback (voice: alloy)")
                
                fallback_voice = 'alloy'  # Neutral, works well with Vietnamese
                client = OpenAI(api_key=settings.OPENAI_API_KEY)
                filename = f"tts_fallback_{uuid.uuid4()}.mp3"
                output_dir = os.path.join(settings.MEDIA_ROOT, "generated_audio")
                os.makedirs(output_dir, exist_ok=True)
                output_path = os.path.join(output_dir, filename)
                
                response = client.audio.speech.create(
                    model="tts-1",
                    voice=fallback_voice,
                    input=text
                )
                response.stream_to_file(output_path)
                
                relative_path = os.path.relpath(output_path, settings.MEDIA_ROOT)
                audio_url = settings.MEDIA_URL + relative_path.replace('\\', '/')
                
                logger.info(f"OpenAI TTS fallback audio generated: {filename}")
                return JsonResponse({
                    'success': True,
                    'data': {
                        'audio_url': audio_url,
                        'fallback': True,
                        'fallback_note': 'Đã dùng giọng OpenAI (alloy) vì HeyGen không hỗ trợ TTS trực tiếp. Nếu muốn đúng giọng clone, hãy dùng Upload Audio.'
                    }
                })
                
            except Exception as e:
                logger.error(f"TTS Fallback Error: {str(e)}")
                return JsonResponse({'success': False, 'error': f"Lỗi tạo audio: {str(e)}"}, status=500)

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
