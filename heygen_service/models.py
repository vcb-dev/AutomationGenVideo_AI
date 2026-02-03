"""
Pydantic models for HeyGen API requests and responses
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum


class VideoQuality(str, Enum):
    """Video quality options"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    ULTRA = "ultra"


class VideoAspectRatio(str, Enum):
    """Video aspect ratio options"""
    LANDSCAPE = "16:9"  # YouTube, Facebook
    PORTRAIT = "9:16"   # TikTok, Instagram Reels
    SQUARE = "1:1"      # Instagram Feed


class VoiceSettings(BaseModel):
    """Voice configuration for video"""
    type: str = Field(default="text", description="text or audio")
    voice_id: Optional[str] = Field(default=None, description="HeyGen voice ID for TTS")
    input_text: Optional[str] = Field(default=None, description="Text to convert to speech")
    input_audio_url: Optional[str] = Field(default=None, description="URL to audio file")
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="Speech speed")


class VideoGenerationRequest(BaseModel):
    """Request model for generating video with HeyGen"""
    
    # Avatar Configuration
    avatar_id: str = Field(..., description="HeyGen avatar ID (brand face)")
    avatar_style: str = Field(default="normal", description="Avatar style (e.g., normal, professional, energetic)")
    
    # Voice/Audio Input
    voice_settings: VoiceSettings
    
    # Video Settings
    quality: VideoQuality = Field(default=VideoQuality.MEDIUM)
    aspect_ratio: VideoAspectRatio = Field(default=VideoAspectRatio.LANDSCAPE)
    
    # Background (optional)
    background_color: Optional[str] = Field(default="#FFFFFF", description="Hex color")
    background_image_url: Optional[str] = Field(default=None, description="Background image URL")
    
    # Metadata
    title: Optional[str] = Field(default=None, description="Video title for tracking")
    
    class Config:
        json_schema_extra = {
            "example": {
                "avatar_id": "avatar_abc123xyz",
                "voice_settings": {
                    "type": "text",
                    "input_text": "Xin chào! Tôi là đại diện thương hiệu Viễn Chí Bảo.",
                    "voice_id": "vi-VN-female-1",
                    "speed": 1.0
                },
                "quality": "high",
                "aspect_ratio": "16:9",
                "background_color": "#F5F5F5",
                "title": "Brand Introduction Video"
            }
        }


class VideoGenerationResponse(BaseModel):
    """Response model from HeyGen API"""
    
    video_id: str = Field(..., description="HeyGen video ID")
    status: str = Field(..., description="pending, processing, completed, failed")
    video_url: Optional[str] = Field(default=None, description="URL to download video (when completed)")
    thumbnail_url: Optional[str] = Field(default=None, description="Video thumbnail")
    duration: Optional[float] = Field(default=None, description="Video duration in seconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    created_at: str = Field(..., description="Creation timestamp")
    
    # Metadata
    callback_id: Optional[str] = Field(default=None, description="Custom callback ID")
    
    class Config:
        json_schema_extra = {
            "example": {
                "video_id": "vid_xyz789abc",
                "status": "completed",
                "video_url": "https://heygen-videos.s3.amazonaws.com/vid_xyz789abc.mp4",
                "thumbnail_url": "https://heygen-videos.s3.amazonaws.com/vid_xyz789abc_thumb.jpg",
                "duration": 45.5,
                "created_at": "2026-02-02T13:00:00Z"
            }
        }


class VideoStatusResponse(BaseModel):
    """Response model for checking video status"""
    
    video_id: str
    status: str
    progress: Optional[int] = Field(default=None, ge=0, le=100, description="Processing progress %")
    video_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    duration: Optional[float] = None
    error: Optional[Any] = Field(default=None, description="Error message or object if failed")
    estimated_time: Optional[int] = Field(default=None, description="Estimated completion time in seconds")
