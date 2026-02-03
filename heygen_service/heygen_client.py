"""
HeyGen API Client
Official API wrapper for HeyGen video generation
Documentation: https://docs.heygen.com/reference/api-overview
"""

import os
import aiohttp
import asyncio
import logging
import json
from typing import Optional, Dict, Any
from .models import (
    VideoGenerationRequest,
    VideoGenerationResponse,
    VideoStatusResponse,
    VoiceSettings
)

logger = logging.getLogger(__name__)


class HeyGenClient:
    """
    HeyGen API Client for AI Avatar video generation
    
    Features:
    - Create videos with custom avatars
    - Text-to-speech or audio input
    - Check video status
    - Download completed videos
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        test_mode: Optional[bool] = None
    ):
        """
        Initialize HeyGen client
        
        Args:
            api_key: HeyGen API key (or from env HEYGEN_API_KEY)
            api_url: API base URL (or from env HEYGEN_API_URL)
            test_mode: Test mode (watermarked, free) or from env HEYGEN_TEST_MODE
        """
        self.api_key = api_key or os.getenv("HEYGEN_API_KEY")
        self.api_url = api_url or os.getenv("HEYGEN_API_URL", "https://api.heygen.com/v2")
        
        # Test mode: True = watermarked (FREE), False = production (costs credits)
        if test_mode is None:
            test_mode_env = os.getenv("HEYGEN_TEST_MODE", "True")
            self.test_mode = test_mode_env.lower() in ("true", "1", "yes")
        else:
            self.test_mode = test_mode
        
        if not self.api_key:
            raise ValueError("HeyGen API key is required. Set HEYGEN_API_KEY in .env")
        
        self.headers = {
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json"
        }
        
        mode_str = "TEST (watermarked, FREE)" if self.test_mode else "PRODUCTION (costs credits)"
        logger.info(f"HeyGen client initialized - Mode: {mode_str}, API URL: {self.api_url}")
    
    async def create_video(
        self,
        request: VideoGenerationRequest,
        callback_id: Optional[str] = None
    ) -> VideoGenerationResponse:
        """
        Create a new video with avatar and voice
        
        Args:
            request: Video generation request with avatar and voice settings
            callback_id: Optional callback ID for tracking
        
        Returns:
            VideoGenerationResponse with video_id and status
        
        Raises:
            Exception: If API request fails
        """
        endpoint = f"{self.api_url}/video/generate"
        
        # Build request payload
        # Route "photo" types to Talking Photo method, but treat known Video Avatar IDs as standard
        if "photo" in request.avatar_id.lower() or "talking" in request.avatar_id.lower():
             return await self.create_talking_photo_video(request)

        # Standard Avatar Payload
        payload = {
            "video_inputs": [
                {
                    "character": {
                        "type": "avatar",
                        "avatar_id": request.avatar_id,
                        "avatar_style": request.avatar_style or "normal"
                    },
                    "voice": self._build_voice_payload(request.voice_settings),
                    "background": self._build_background_payload(request)
                }
            ],
            "dimension": {
                "width": self._get_width(request.aspect_ratio),
                "height": self._get_height(request.aspect_ratio)
            },
            "test": self.test_mode,
            "caption": False,
            "quality": "medium"
        }
        
        logger.info(f"Sending Video Payload: {json.dumps(payload, ensure_ascii=False)}")
        
        if callback_id:
            payload["callback_id"] = callback_id
        
        if request.title:
            payload["title"] = request.title
        
        logger.info(f"Creating video with avatar: {request.avatar_id}")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint,
                headers=self.headers,
                json=payload
            ) as response:
                response_data = await response.json()
                
                if response.status != 200:
                    error_msg = response_data.get("error", {}).get("message", "Unknown error")
                    logger.error(f"HeyGen API error: {error_msg}")
                    
                    # If error suggests avatar not found, try Talking Photo method
                    # DISABLE FALLBACK FOR NOW to verify standard avatar works
                    # if "not found" in error_msg.lower() or "available" in error_msg.lower() or "exist" in error_msg.lower():
                    #     logger.info("Avatar not found as standard avatar. Retrying as Talking Photo...")
                    #     return await self.create_talking_photo_video(request)
                        
                    raise Exception(f"HeyGen API error: {error_msg}")
                
                # Parse response
                data = response_data.get("data", {})
                
                return VideoGenerationResponse(
                    video_id=data.get("video_id"),
                    status="pending",
                    created_at=data.get("created_at", ""),
                    callback_id=callback_id
                )
                
    async def create_talking_photo_video(
        self,
        request: VideoGenerationRequest
    ) -> VideoGenerationResponse:
        """Create video using Talking Photo API"""
        # Talking Photo API is V2 (or V1) but uses different endpoint/payload
        # For now, we will try to use the specific endpoint for Talking Photos if available
        # But actually, HeyGen V2 Unified API suggests sticking to /video/generate but changing character type
        
        # Reverting to V2 API (Unofficial/Unified) but with specific Talking Photo payload
        endpoint = f"{self.api_url}/video/generate"
        
        payload = {
            "video_inputs": [
                {
                    "character": {
                        "type": "talking_photo",
                        "talking_photo_id": request.avatar_id
                    },
                    "voice": self._build_voice_payload(request.voice_settings),
                    "background": {
                        "type": "color",
                        "value": request.background_color or "#FFFFFF"
                    }
                }
            ],
            "dimension": {
                 "width": self._get_width(request.aspect_ratio),
                 "height": self._get_height(request.aspect_ratio)
            },
            "test": self.test_mode,
            "caption": False
        }
        
        if request.title:
            payload["title"] = request.title
            
        logger.info(f"Attempting Talking Photo generation for ID: {request.avatar_id}")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, headers=self.headers, json=payload) as response:
                resp_data = await response.json()
                if response.status != 200:
                    error_msg = resp_data.get("error", {}).get("message", "Unknown error")
                    logger.error(f"Talking Photo API Error: {error_msg}")
                    raise Exception(f"Talking Photo Error: {error_msg}")
                
                data = resp_data.get("data", {})
                return VideoGenerationResponse(
                    video_id=data.get("video_id"),
                    status="pending",
                    created_at=data.get("created_at", "")
                )

    async def get_video_status(self, video_id: str) -> VideoStatusResponse:
        """
        Check video generation status
        
        Args:
            video_id: HeyGen video ID
        
        Returns:
            VideoStatusResponse with current status and progress
        """
        # v2 API often has issues with status check or moves it, falling back to v1 for status check which works reliably
        endpoint = "https://api.heygen.com/v1/video_status.get"
        
        params = {"video_id": video_id}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                endpoint,
                headers=self.headers,
                params=params
            ) as response:
                response_data = await response.json()
                
                if response.status != 200:
                    error_msg = response_data.get("error", {}).get("message", "Unknown error")
                    raise Exception(f"Failed to get video status: {error_msg}")
                
                data = response_data.get("data", {})
                
                return VideoStatusResponse(
                    video_id=video_id,
                    status=data.get("status", "unknown"),
                    progress=data.get("progress"),
                    video_url=data.get("video_url"),
                    thumbnail_url=data.get("thumbnail_url"),
                    duration=data.get("duration"),
                    error=data.get("error"),
                    estimated_time=data.get("estimated_time")
                )
    
    async def wait_for_completion(
        self,
        video_id: str,
        timeout: int = 600,
        poll_interval: int = 5
    ) -> VideoStatusResponse:
        """
        Wait for video to complete processing
        
        Args:
            video_id: HeyGen video ID
            timeout: Maximum wait time in seconds (default: 10 minutes)
            poll_interval: Check interval in seconds (default: 5s)
        
        Returns:
            VideoStatusResponse when completed or failed
        
        Raises:
            TimeoutError: If video doesn't complete within timeout
        """
        start_time = asyncio.get_event_loop().time()
        
        while True:
            status = await self.get_video_status(video_id)
            
            if status.status in ["completed", "failed"]:
                return status
            
            # Check timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Video generation timeout after {timeout}s")
            
            logger.info(f"Video {video_id} status: {status.status} ({status.progress}%)")
            await asyncio.sleep(poll_interval)
    
    def _build_voice_payload(self, voice_settings: VoiceSettings) -> Dict[str, Any]:
        """Build voice payload for API request"""
        if voice_settings.type == "text":
            return {
                "type": "text",
                "input_text": voice_settings.input_text,
                "voice_id": voice_settings.voice_id,
                "speed": voice_settings.speed
            }
        else:  # audio
            return {
                "type": "audio",
                "audio_url": voice_settings.input_audio_url,
                "speed": voice_settings.speed
            }
    
    def _build_background_payload(self, request: VideoGenerationRequest) -> Dict[str, Any]:
        """Build background payload"""
        if request.background_image_url:
            return {
                "type": "image",
                "url": request.background_image_url
            }
        else:
            return {
                "type": "color",
                "value": request.background_color
            }
    
    def _get_width(self, aspect_ratio: str) -> int:
        """Get video width based on aspect ratio"""
        # Using 720p dimensions for Free/Trial plan compatibility
        ratio_map = {
            "16:9": 1280,   # Landscape (was 1920)
            "9:16": 720,    # Portrait (was 1080)
            "1:1": 720      # Square (was 1080)
        }
        return ratio_map.get(aspect_ratio, 1280)
    
    def _get_height(self, aspect_ratio: str) -> int:
        """Get video height based on aspect ratio"""
        # Using 720p dimensions for Free/Trial plan compatibility
        ratio_map = {
            "16:9": 720,    # Landscape (was 1080)
            "9:16": 1280,   # Portrait (was 1920)
            "1:1": 720      # Square (was 1080)
        }
        return ratio_map.get(aspect_ratio, 720)
    
    async def download_video(self, video_url: str, output_path: str) -> str:
        """
        Download completed video
        
        Args:
            video_url: URL to video file
            output_path: Local path to save video
        
        Returns:
            Path to downloaded file
        """
        async with aiohttp.ClientSession() as session:
            async with session.get(video_url) as response:
                if response.status != 200:
                    raise Exception(f"Failed to download video: HTTP {response.status}")
                
                with open(output_path, 'wb') as f:
                    while True:
                        chunk = await response.content.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
        
        logger.info(f"Video downloaded to: {output_path}")
        return output_path


# Convenience function for quick video generation
async def generate_video_from_text(
    text: str,
    avatar_id: Optional[str] = None,
    avatar_style: str = "normal",
    voice_id: Optional[str] = None,
    aspect_ratio: str = "16:9",
    wait_for_completion: bool = True
) -> VideoGenerationResponse:
    """
    Quick helper to generate video from text
    
    Args:
        text: Text to convert to speech
        avatar_id: Avatar ID (or from env HEYGEN_AVATAR_ID)
        voice_id: Voice ID for TTS
        aspect_ratio: Video aspect ratio (16:9, 9:16, 1:1)
        wait_for_completion: Wait for video to complete
    
    Returns:
        VideoGenerationResponse
    """
    client = HeyGenClient()
    
    avatar_id = avatar_id or os.getenv("HEYGEN_AVATAR_ID")
    if not avatar_id:
        raise ValueError("Avatar ID required. Set HEYGEN_AVATAR_ID in .env")
    
    request = VideoGenerationRequest(
        avatar_id=avatar_id,
        avatar_style=avatar_style,
        voice_settings=VoiceSettings(
            type="text",
            input_text=text,
            voice_id=voice_id
        ),
        aspect_ratio=aspect_ratio
    )
    
    response = await client.create_video(request)
    
    if wait_for_completion:
        status = await client.wait_for_completion(response.video_id)
        response.status = status.status
        response.video_url = status.video_url
        response.thumbnail_url = status.thumbnail_url
        response.duration = status.duration
    
    return response
