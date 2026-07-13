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

    # speech-2.8-hd: model HD mới nhất (01/2026), chất lượng + độ giống giọng clone
    # cao nhất. Nếu giọng clone nghe chưa giống, thử "speech-2.6-hd" (được MiniMax
    # ghi nhận "excellent cloning similarity") qua env MINIMAX_TTS_MODEL.
    DEFAULT_MODEL = "speech-2.8-hd"

    def __init__(self, api_key: Optional[str] = None, group_id: Optional[str] = None):
        """
        Initialize Minimax TTS Service.
        
        Args:
            api_key: Minimax API Key (JWT token)
            group_id: Minimax Group ID
        """
        self.api_key = api_key or os.getenv('MINIMAX_API_KEY')
        # Key kiểu mới "sk-api-..." tự gắn với group — KHÔNG cần GroupId; gửi kèm
        # GroupId của account khác sẽ lỗi 1004 "token not match group". Chỉ key JWT
        # kiểu cũ (eyJ...) mới cần. Vì vậy group_id là tùy chọn.
        self.group_id = group_id or os.getenv('MINIMAX_GROUP_ID')
        self.model = os.getenv('MINIMAX_TTS_MODEL', self.DEFAULT_MODEL)

        if not self.api_key:
            raise ValueError("MINIMAX_API_KEY is required")

        group_note = f"Group: {self.group_id[:20]}..." if self.group_id else "Group: (none — sk-api key)"
        logger.info(f"✅ Minimax TTS Service initialized ({group_note}, Model: {self.model})")
    
    def generate_audio(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: int = 0,
        emotion: Optional[str] = None,
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
            emotion: Emotion type ("happy", "sad", "calm", ...). Bỏ trống để MiniMax
                     tự chọn cảm xúc tự nhiên theo nội dung văn bản (khuyên dùng —
                     ép cứng 1 emotion cho mọi câu dễ làm giọng đọc bị "kịch"/méo).
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
            voice_setting = {
                "voice_id": voice_id,
                "speed": speed,
                "vol": vol,
                "pitch": pitch,
            }
            # Chỉ gửi emotion khi caller chỉ định — bỏ trống để MiniMax tự chọn
            # cảm xúc tự nhiên theo văn bản (docs: "automatically selects the most
            # natural emotion based on text").
            if emotion:
                voice_setting["emotion"] = emotion

            payload = {
                "model": self.model,
                "text": text,
                "stream": False,
                "voice_setting": voice_setting,
                # Chất lượng cao nhất cho MP3: 44.1kHz / 256kbps
                "audio_setting": {
                    "sample_rate": 44100,
                    "bitrate": 256000,
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
            
            # Chỉ gắn GroupId khi có (key JWT cũ); key sk-api không cần
            url = f"{self.BASE_URL}?GroupId={self.group_id}" if self.group_id else self.BASE_URL
            
            logger.info(f"📡 Calling Minimax API: {url[:50]}...")

            # Make request — retry khi lỗi tầng mạng. Kết nối tới api.minimax.io
            # chập chờn (cả 2 IP load-balancer, xác nhận 2026-07-07): 1 request treo
            # 300s vượt luôn quota chờ của BE. Thay bằng 5 lần thử x 45s (~225s max,
            # dưới trần 300s của BE); TTS thật chỉ mất 3-10s nên 45s/lần là quá đủ.
            start_time = time.time()
            last_network_error = None
            response = None
            for attempt in range(1, 6):
                try:
                    response = requests.post(url, headers=headers, json=payload, timeout=45)
                    last_network_error = None
                    break
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as net_err:
                    last_network_error = net_err
                    logger.warning(f"📡 Minimax TTS attempt {attempt}/5 failed (network): {net_err}")
            if response is None:
                raise last_network_error if last_network_error else RuntimeError("TTS retry loop exited without result")

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


def get_minimax_service(api_key: Optional[str] = None) -> MinimaxTTSService:
    """
    Get or create Minimax service instance.

    api_key: key do BE gửi kèm từng request qua header X-Minimax-Key — key MiniMax
    lưu ở .env của BE, không còn lưu ở .env AI. Có api_key thì tạo instance riêng
    (không cache vào singleton để key của request này không rò sang request khác);
    singleton + env chỉ còn là fallback cho management command chạy tay.
    """
    global _minimax_service
    if api_key:
        return MinimaxTTSService(api_key=api_key)
    if _minimax_service is None:
        _minimax_service = MinimaxTTSService()
    return _minimax_service
