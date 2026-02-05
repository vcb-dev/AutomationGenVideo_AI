"""
Mock HeyGen Client for Development (No API calls)
Use this when you've hit the daily limit but still need to test UI
"""

import logging
import asyncio
from typing import Optional
from .models import VideoGenerationRequest, VideoGenerationResponse, VideoStatusResponse

logger = logging.getLogger(__name__)


class MockHeyGenClient:
    """
    Mock HeyGen client that simulates API responses without making real calls.
    Useful for development when you've hit the daily API limit.
    """
    
    def __init__(self, *args, **kwargs):
        logger.warning("🎭 MOCK MODE: Using MockHeyGenClient - No real API calls will be made")
        self.test_mode = True
    
    async def create_video(
        self,
        request: VideoGenerationRequest,
        callback_id: Optional[str] = None
    ) -> VideoGenerationResponse:
        """Simulate video creation"""
        logger.info(f"🎭 MOCK: Creating video with text: {request.voice_settings.input_text[:50]}...")
        
        # Simulate API delay
        await asyncio.sleep(1)
        
        # Return mock response
        return VideoGenerationResponse(
            video_id="mock_video_" + str(hash(request.voice_settings.input_text))[:8],
            status="pending",
            created_at="2026-02-03T12:00:00Z",
            callback_id=callback_id
        )
    
    async def create_talking_photo_video(
        self,
        request: VideoGenerationRequest
    ) -> VideoGenerationResponse:
        """Simulate talking photo creation"""
        return await self.create_video(request)
    
    async def get_video_status(self, video_id: str) -> VideoStatusResponse:
        """Simulate video status check"""
        logger.info(f"🎭 MOCK: Checking status for {video_id}")
        
        # Simulate processing
        await asyncio.sleep(0.5)
        
        # Return completed status with mock video URL
        return VideoStatusResponse(
            video_id=video_id,
            status="completed",
            progress=100,
            video_url="https://example.com/mock_video.mp4",
            thumbnail_url="https://example.com/mock_thumbnail.jpg",
            duration=30,
            error=None,
            estimated_time=None
        )
    
    async def wait_for_completion(
        self,
        video_id: str,
        timeout: int = 600,
        poll_interval: int = 5
    ) -> VideoStatusResponse:
        """Simulate waiting for completion"""
        logger.info(f"🎭 MOCK: Simulating video processing for {video_id}...")
        
        # Simulate processing time
        await asyncio.sleep(2)
        
        return await self.get_video_status(video_id)
    
    async def download_video(self, video_url: str, output_path: str) -> str:
        """Simulate video download"""
        logger.info(f"🎭 MOCK: Simulating download to {output_path}")
        
        # Create empty file
        with open(output_path, 'w') as f:
            f.write("Mock video file")
        
        return output_path
