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
from .ai_writer import AIWriter

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
        
        # HOTFIX: If old ID is sent (due to frontend cache), swap it with the new correct ID
        if avatar_id == '871cc4b87b8643c6b9cc6b4cf6797fc9':
            avatar_id = 'c73ebf9cd4ea4d0e9764ecf684999d71'

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
        
        # Create video generation request
        logger.info(f"Generating video with text: {text[:50]}...")
        
        # Run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            response = loop.run_until_complete(
                generate_video_from_text(
                    text=text,
                    voice_id=voice_id,
                    avatar_id=avatar_id,
                    avatar_style=avatar_style,
                    aspect_ratio=aspect_ratio,
                    wait_for_completion=False  # Don't wait, return immediately
                )
            )
            
            return JsonResponse({
                'success': True,
                'data': {
                    'video_id': response.video_id,
                    'status': response.status,
                    'message': 'Video generation started. Use /status endpoint to check progress.'
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
    # Hardcoded voice list (HeyGen doesn't have a voices API)
    # These are common voice IDs based on HeyGen documentation
    voices = [
        {
            "id": "c6fb81520dcd42e0a02be231046a8639",
            "name": "Nam Minh (Natural)",
            "language": "vi-VN",
            "gender": "male"
        },
        {
            "id": "4286c03d11f44af093e379fc7e2cafa6",
            "name": "Chau (Natural)",
            "language": "vi-VN",
            "gender": "female"
        },
        {
            "id": "9a247a37f3c04e6aa934171998b9659c",
            "name": "Hoai (Natural)",
            "language": "vi-VN",
            "gender": "female"
        },
        {
            "id": "en-US-female-1",
            "name": "English Female 1",
            "language": "en-US",
            "gender": "female"
        },
        {
            "id": "en-US-male-1",
            "name": "English Male 1",
            "language": "en-US",
            "gender": "male"
        }
    ]
    
    return JsonResponse({
        'success': True,
        'data': {
            'voices': voices
        }
    })
