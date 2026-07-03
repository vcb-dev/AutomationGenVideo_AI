"""
Minimax TTS Service - Text-to-Speech using Minimax AI.

API Docs: https://platform.minimaxi.com/document/T2A%20V2?key=66719005a427f0c8a5701643
"""

import logging
import requests
import os
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class MinimaxTTSService:
    """
    Service for generating audio using Minimax TTS API.
    """
    
    BASE_URL = "https://api.minimax.io/v1/t2a_v2"
    
    def __init__(self, api_key: Optional[str] = None, group_id: Optional[str] = None):
        """
        Initialize Minimax TTS Service.
        
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
        
        logger.info(f"✅ Minimax TTS Service initialized (Group: {self.group_id[:20]}...)")
    
    def generate_audio(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: int = 0,
        emotion: str = "happy",
        language_boost: Optional[str] = None,
        output_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate audio from text using Minimax TTS.

        Args:
            text: Text to convert to speech
            voice_id: Minimax voice ID (e.g., "moss_audio_ce3450f9-c782-11f0-a527-aab150a40f84")
            speed: Speech speed (0.5 - 2.0), default 1.0
            vol: Volume (0.1 - 10.0), default 1.0
            pitch: Pitch adjustment (-12 to 12), default 0
            emotion: Emotion type ("happy", "sad", "angry", "neutral", etc.)
            language_boost: Minimax language hint (e.g. "Vietnamese", "English", "auto")
            output_path: Optional local path to save audio file

        Returns:
            Dictionary with:
            {
                'success': bool,
                'audio_url': str,          # URL to download audio
                'file_path': str,          # Local path if output_path provided
                'duration': float,         # Audio duration in seconds
                'extra_info': dict
            }
        """
        try:
            logger.info(f"🎤 Minimax TTS - Voice: {voice_id}, Text: {len(text)} chars")
            
            # Build request payload
            payload = {
                "model": "speech-02-turbo",  # Updated to latest model
                "text": text,
                "stream": False,
                "voice_setting": {
                    "voice_id": voice_id,
                    "speed": speed,
                    "vol": vol,
                    "pitch": pitch,
                    "emotion": emotion
                },
                "audio_setting": {
                    "sample_rate": 32000,
                    "bitrate": 128000,
                    "format": "mp3",
                    "channel": 1
                }
            }
            if language_boost:
                payload["language_boost"] = language_boost
            
            # Build headers
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            # Build URL with group_id
            url = f"{self.BASE_URL}?GroupId={self.group_id}"
            
            logger.info(f"📡 Calling Minimax API: {url[:50]}...")
            
            # Make request
            start_time = time.time()
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=300  # 5 minutes for long text
            )
            
            elapsed = time.time() - start_time
            logger.info(f"📡 Minimax Response: Status={response.status_code} in {elapsed:.1f}s")
            
            if response.status_code != 200:
                logger.error(f"❌ Minimax API failed: {response.text}")
                raise Exception(f"Minimax API error ({response.status_code}): {response.text}")
            
            data = response.json()
            logger.info(f"✅ Minimax Response: {data.keys()}")
            
            # Extract audio from response. Tuỳ model/account Minimax trả về một trong:
            #   {"data": {"audio": "<hex>"}, "extra_info": {...}}        (t2a_v2 chính thức — hex)
            #   {"audio_file": "<base64 hoặc url>", "extra_info": {...}} (biến thể cũ)
            inner = data.get('data') or {}
            audio_file = (
                data.get('audio_file')
                or inner.get('audio_file')
                or inner.get('audio')
                or data.get('audio')
            )
            extra_info = data.get('extra_info', {})

            if not audio_file:
                logger.error(f"❌ No audio in response: {data}")
                raise Exception(f"No audio in Minimax response: {data}")

            if audio_file.startswith('http'):
                audio_url = audio_file
                logger.info(f"✅ Audio URL: {audio_url[:100]}...")
                # Caller (voice_tts_api) serves the file from output_path via /media —
                # nếu không tải về thì URL /media trả 404. Download về output_path.
                if output_path:
                    dl = requests.get(audio_file, timeout=120)
                    dl.raise_for_status()
                    with open(output_path, 'wb') as f:
                        f.write(dl.content)
                    logger.info(f"✅ Audio downloaded to: {output_path}")
            else:
                # Chuỗi audio nhúng: t2a_v2 trả hex, biến thể cũ trả base64 — thử hex trước.
                import base64
                try:
                    audio_bytes = bytes.fromhex(audio_file)
                except ValueError:
                    audio_bytes = base64.b64decode(audio_file)

                if not output_path:
                    # Generate temp path
                    import tempfile
                    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
                    output_path = temp_file.name
                    temp_file.close()

                with open(output_path, 'wb') as f:
                    f.write(audio_bytes)

                audio_url = output_path
                logger.info(f"✅ Audio saved to: {output_path}")
            
            # Get duration from extra_info
            duration = extra_info.get('audio_length', 0) or extra_info.get('duration', 0)
            
            result = {
                'success': True,
                'audio_url': audio_url,
                'file_path': output_path if output_path else None,
                'duration': duration,
                'extra_info': extra_info
            }
            
            logger.info(f"✅ Minimax TTS Success - Duration: {duration}s")
            return result
            
        except Exception as e:
            logger.error(f"❌ Minimax TTS Error: {str(e)}", exc_info=True)
            raise


# Singleton instance
_minimax_service = None


def get_minimax_service() -> MinimaxTTSService:
    """Get or create Minimax service instance."""
    global _minimax_service
    if _minimax_service is None:
        _minimax_service = MinimaxTTSService()
    return _minimax_service
