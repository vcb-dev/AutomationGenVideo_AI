"""
Minimax Voice Cloning Service - Clone custom voices from audio files.

API Docs: https://platform.minimax.io/docs/api-reference/voice-cloning-intro
"""

import logging
import re
import requests
import os
import uuid
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def _make_voice_id(voice_name: str) -> str:
    """
    Generate valid Minimax voice_id from voice_name.
    Rules: length 8-256, start with letter, only [a-zA-Z0-9_-], cannot end with - or _

    A random suffix is always appended so two concurrent clone requests with the same
    voice_name (e.g. a double-submit) don't collide on the same voice_id.
    """
    # Replace spaces/special with underscore, keep only letters digits _ -
    s = re.sub(r'[^a-zA-Z0-9_\-]', '_', voice_name.strip())
    s = re.sub(r'_+', '_', s).strip('_')
    if not s or not s[0].isalpha():
        s = 'v_' + s if s else 'voice_01'
    s = f"{s}_{uuid.uuid4().hex[:8]}"
    if len(s) > 256:
        s = s[:256]
    if s.endswith('_') or s.endswith('-'):
        s = s.rstrip('_-') or 'voice_01'
    return s


class MinimaxVoiceCloneService:
    """
    Service for cloning custom voices using Minimax Voice Cloning API.
    """
    
    BASE_URL = "https://api.minimax.io/v1"
    
    def __init__(self, api_key: Optional[str] = None, group_id: Optional[str] = None):
        """
        Initialize Minimax Voice Clone Service.
        
        Args:
            api_key: Minimax API Key (JWT token)
            group_id: Minimax Group ID
        """
        self.api_key = api_key or os.getenv('MINIMAX_API_KEY')
        self.group_id = group_id or os.getenv('MINIMAX_GROUP_ID')
        
        if not self.api_key:
            raise ValueError("MINIMAX_API_KEY is required")
        if not self.group_id:
            raise ValueError("MINIMAX_GROUP_ID is required")
        
        logger.info(f"[Voice Clone] Minimax service initialized (Group: {self.group_id[:20]}...)")
    
    def upload_audio(self, audio_path: str, purpose: str = "voice_clone") -> str:
        """
        Upload audio file to Minimax.
        
        Args:
            audio_path: Path to audio file (mp3, m4a, wav)
            purpose: Purpose of upload ("voice_clone" or "prompt")
            
        Returns:
            file_id: ID of uploaded file
        """
        try:
            logger.info(f"[Voice Clone] Uploading audio: {audio_path}")
            
            # Check file exists
            if not os.path.exists(audio_path):
                raise FileNotFoundError(f"Audio file not found: {audio_path}")
            
            # Get file size
            file_size = os.path.getsize(audio_path)
            logger.info(f"[Voice Clone] File size: {file_size / (1024*1024):.2f} MB")
            
            if file_size > 20 * 1024 * 1024:  # 20MB
                raise ValueError("File size must be less than 20MB")
            
            # Build URL with GroupId
            url = f"{self.BASE_URL}/files/upload?GroupId={self.group_id}"
            
            # Build headers (no Content-Type for multipart/form-data)
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }
            
            # Upload file
            with open(audio_path, 'rb') as f:
                files = {
                    'file': (os.path.basename(audio_path), f, 'audio/mpeg'),
                    'purpose': (None, purpose)
                }
                
                logger.info(f"[Voice Clone] Uploading to: {url[:60]}...")
                response = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    timeout=120
                )
            
            logger.info(f"[Voice Clone] Upload response: Status={response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"[Voice Clone] Upload failed: {response.text}")
                raise Exception(f"Upload failed ({response.status_code}): {response.text}")
            
            data = response.json()
            logger.info(f"[Voice Clone] Upload response data: {data}")
            
            # Extract file_id (API returns file_id inside data['file'] or data['data'])
            file_obj = data.get('file') or data.get('data', {})
            file_id = file_obj.get('file_id') if isinstance(file_obj, dict) else None
            if not file_id:
                file_id = data.get('file_id')
            # file_id may be int from API
            if file_id is not None:
                file_id = str(file_id)
            
            if not file_id:
                raise Exception(f"No file_id in response: {data}")
            
            logger.info(f"[Voice Clone] Upload success! file_id: {file_id}")
            return file_id
            
        except Exception as e:
            logger.error(f"[Voice Clone] Upload error: {str(e)}", exc_info=True)
            raise
    
    def clone_voice(
        self,
        file_id: str,
        voice_name: str,
        voice_id: Optional[str] = None,
        prompt_file_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Clone voice from uploaded audio.
        
        Args:
            file_id: ID of uploaded source audio
            voice_name: Name for the cloned voice
            voice_id: Custom voice ID (optional, will be generated if not provided)
            prompt_file_id: Optional ID of uploaded prompt audio for better quality
            
        Returns:
            Dictionary with:
            {
                'voice_id': str,
                'voice_name': str,
                'status': str,
                'expires_at': int  # Unix timestamp (168 hours from now)
            }
        """
        try:
            # API requires voice_id (string, 8-256 chars, start with letter). No voice_name in API.
            result_voice_id = voice_id or _make_voice_id(voice_name)
            logger.info(f"[Voice Clone] Cloning voice: {voice_name} -> voice_id: {result_voice_id}")
            logger.info(f"[Voice Clone] Source file_id: {file_id}")
            
            # file_id must be integer per Minimax API
            file_id_int = int(file_id) if isinstance(file_id, str) else file_id
            
            # Build payload per https://platform.minimax.io/docs/api-reference/voice-cloning-clone
            payload = {
                "file_id": file_id_int,
                "voice_id": result_voice_id,
            }
            
            if prompt_file_id:
                pid = int(prompt_file_id) if isinstance(prompt_file_id, str) else prompt_file_id
                payload["clone_prompt"] = {
                    "prompt_audio": pid,
                    "prompt_text": "Sample prompt for voice cloning."
                }
            
            # Build URL with GroupId
            url = f"{self.BASE_URL}/voice_clone?GroupId={self.group_id}"
            
            # Build headers
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            logger.info(f"[Voice Clone] Calling: {url[:60]}...")
            
            # Make request
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=120
            )
            
            logger.info(f"[Voice Clone] Clone response: Status={response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"[Voice Clone] Clone failed: {response.text}")
                raise Exception(f"Clone failed ({response.status_code}): {response.text}")
            
            data = response.json()
            logger.info(f"[Voice Clone] Clone response data: {data}")
            
            base_resp = data.get('base_resp', {})
            status_code = base_resp.get('status_code', -1)
            status_msg = base_resp.get('status_msg', '')
            
            if status_code != 0:
                raise Exception(f"Minimax API error ({status_code}): {status_msg}")
            
            # API returns success without voice_id in body; we use the one we sent
            
            # Calculate expiry (168 hours = 7 days from now)
            import time
            expires_at = int(time.time()) + (168 * 3600)
            
            result = {
                'voice_id': result_voice_id,
                'voice_name': voice_name,
                'status': 'cloned',
                'expires_at': expires_at
            }
            
            logger.info(f"[Voice Clone] Clone success! voice_id: {result_voice_id}")
            return result
            
        except Exception as e:
            logger.error(f"[Voice Clone] Clone error: {str(e)}", exc_info=True)
            raise
    
    def clone_voice_from_file(
        self,
        audio_path: str,
        voice_name: str,
        voice_id: Optional[str] = None,
        prompt_audio_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Complete workflow: Upload audio + Clone voice.
        
        Args:
            audio_path: Path to source audio file (10s - 5min, mp3/m4a/wav, max 20MB)
            voice_name: Name for the cloned voice
            voice_id: Custom voice ID (optional)
            prompt_audio_path: Optional path to prompt audio (<8s) for better quality
            
        Returns:
            Dictionary with voice_id and other info
        """
        try:
            logger.info(f"[Voice Clone] Starting voice clone workflow...")
            logger.info(f"[Voice Clone] Audio: {audio_path}")
            logger.info(f"[Voice Clone] Name: {voice_name}")
            
            # Step 1: Upload source audio
            file_id = self.upload_audio(audio_path, purpose="voice_clone")
            
            # Step 2: Upload prompt audio (optional)
            prompt_file_id = None
            if prompt_audio_path:
                logger.info(f"[Voice Clone] Uploading prompt audio: {prompt_audio_path}")
                prompt_file_id = self.upload_audio(prompt_audio_path, purpose="prompt")
            
            # Step 3: Clone voice
            result = self.clone_voice(
                file_id=file_id,
                voice_name=voice_name,
                voice_id=voice_id,
                prompt_file_id=prompt_file_id
            )
            
            logger.info(f"[Voice Clone] Workflow complete! voice_id: {result['voice_id']}")
            return result
            
        except Exception as e:
            logger.error(f"[Voice Clone] Workflow error: {str(e)}", exc_info=True)
            raise


# Singleton instance
_voice_clone_service = None


def get_voice_clone_service() -> MinimaxVoiceCloneService:
    """Get or create Voice Clone service instance."""
    global _voice_clone_service
    if _voice_clone_service is None:
        _voice_clone_service = MinimaxVoiceCloneService()
    return _voice_clone_service
