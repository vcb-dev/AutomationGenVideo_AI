"""
HeyGen Service Module
Handles AI Avatar video generation with lipsync using HeyGen API
"""

from .heygen_client import HeyGenClient
from .models import VideoGenerationRequest, VideoGenerationResponse

__all__ = ['HeyGenClient', 'VideoGenerationRequest', 'VideoGenerationResponse']
