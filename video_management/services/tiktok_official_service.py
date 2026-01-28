"""
TikTok Official API Service for fetching user profile data.

This service uses TikTok's official API for Developers to get accurate
user statistics including followers, likes, and video count.
"""
import logging
import requests
from typing import Dict, Any, Optional
from django.conf import settings

logger = logging.getLogger(__name__)


class TikTokOfficialAPIService:
    """Service for TikTok Official API."""
    
    def __init__(self):
        """Initialize TikTok API service."""
        self.client_key = getattr(settings, 'TIKTOK_CLIENT_KEY', '')
        self.client_secret = getattr(settings, 'TIKTOK_CLIENT_SECRET', '')
        self.base_url = 'https://open.tiktokapis.com/v2'
        
        if not self.client_key or not self.client_secret:
            raise Exception("TikTok API credentials not configured")
        
        logger.info("TikTok Official API service initialized")
    
    def get_access_token(self, code: str) -> str:
        """
        Get access token using authorization code.
        
        Args:
            code: Authorization code from OAuth flow
            
        Returns:
            Access token string
        """
        url = f"{self.base_url}/oauth/token/"
        
        data = {
            'client_key': self.client_key,
            'client_secret': self.client_secret,
            'code': code,
            'grant_type': 'authorization_code'
        }
        
        response = requests.post(url, data=data)
        response.raise_for_status()
        
        return response.json()['data']['access_token']
    
    def get_user_info(self, access_token: str, fields: Optional[list] = None) -> Dict[str, Any]:
        """
        Get user information using access token.
        
        Args:
            access_token: User's access token
            fields: List of fields to retrieve (default: all)
            
        Returns:
            User profile data
        """
        if fields is None:
            fields = [
                'open_id',
                'union_id',
                'avatar_url',
                'display_name',
                'follower_count',
                'following_count',
                'likes_count',
                'video_count'
            ]
        
        url = f"{self.base_url}/user/info/"
        
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        params = {
            'fields': ','.join(fields)
        }
        
        logger.info(f"Fetching TikTok user info with fields: {fields}")
        
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        
        data = response.json()
        
        if data.get('error'):
            raise Exception(f"TikTok API error: {data['error']['message']}")
        
        user_data = data.get('data', {}).get('user', {})
        
        logger.info(f"✅ TikTok API success: {user_data.get('follower_count', 0):,} followers")
        
        return {
            'username': user_data.get('open_id', ''),
            'display_name': user_data.get('display_name', ''),
            'avatar_url': user_data.get('avatar_url', ''),
            'follower_count': user_data.get('follower_count', 0),
            'following_count': user_data.get('following_count', 0),
            'total_likes': user_data.get('likes_count', 0),
            'video_count': user_data.get('video_count', 0),
            'total_views': 0  # Not available in API
        }


def get_tiktok_official_service() -> TikTokOfficialAPIService:
    """Get or create TikTok Official API service instance."""
    return TikTokOfficialAPIService()
