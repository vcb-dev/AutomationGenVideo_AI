"""
Minimax Voice Cloning Service - Clone custom voices from audio files.

API Docs: https://platform.minimax.io/docs/api-reference/voice-cloning-intro
"""

import logging

from .minimax_errors import minimax_error_message
import re
import requests
import os
import uuid
from typing import Optional, Dict, Any

from django.conf import settings

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
    
    def __init__(self, api_key: Optional[str] = None, group_id: Optional[str] = None):
        """
        Initialize Minimax Voice Clone Service.
        
        Args:
            api_key: Minimax API Key (JWT token)
            group_id: Minimax Group ID
        """
        self.api_key = api_key or getattr(settings, 'MINIMAX_API_KEY', '')
        # Key kiểu mới "sk-api-..." tự gắn với group — KHÔNG cần GroupId; gửi kèm
        # GroupId của account khác sẽ lỗi 1004 "token not match group". Chỉ key JWT
        # kiểu cũ (eyJ...) mới cần. Vì vậy group_id là tùy chọn.
        self.group_id = group_id or getattr(settings, 'MINIMAX_GROUP_ID', '')
        self.base_url = getattr(settings, 'MINIMAX_API_BASE_URL', 'https://api.minimax.io/v1')

        if not self.api_key:
            raise ValueError("MINIMAX_API_KEY is required")

        group_note = f"Group: {self.group_id[:20]}..." if self.group_id else "Group: (none — sk-api key)"
        logger.info(f"[Voice Clone] Minimax service initialized ({group_note})")

    def _build_url(self, path: str) -> str:
        """Build API URL; chỉ gắn GroupId khi có (key JWT cũ)."""
        url = f"{self.base_url}{path}"
        return f"{url}?GroupId={self.group_id}" if self.group_id else url

    def _post_with_retry(self, request_fn, max_attempts: int = 10):
        """
        Gọi request_fn() (không tham số, trả về response) với retry khi lỗi tầng
        mạng (timeout/connection reset). Kết nối tới api.minimax.io chập chờn ở cả
        2 IP load-balancer (đã xác nhận qua test thủ công — không phải 1 IP chết cố
        định, mà lúc IP này lỗi lúc IP kia lỗi), nên retry với timeout ngắn hơn mỗi
        lần để xoay vòng nhanh, tăng cơ hội trúng đường truyền đang ổn. Clone chạy
        trong background thread (xem voice_views.clone_voice_start_api) nên không
        còn bị giới hạn bởi timeout của BE/FE — 10 lần thử x 30s = tối đa ~5
        phút/bước là chấp nhận được để "vượt" qua một đợt mạng xấu kéo dài.
        """
        last_network_error: Optional[BaseException] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return request_fn()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as net_err:
                last_network_error = net_err
                logger.warning(f"[Voice Clone] Attempt {attempt}/{max_attempts} failed (network): {net_err}")
        if last_network_error is None:
            raise RuntimeError("_post_with_retry: exited loop without a result or error")
        raise last_network_error

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
            
            url = self._build_url("/files/upload")
            
            # Build headers (no Content-Type for multipart/form-data)
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }
            
            # Upload file. Mạng tới api.minimax.io thỉnh thoảng bị nghẽn thoáng qua
            # (read timeout) — retry tối đa 3 lần cho lỗi tầng mạng. Upload lặp lại
            # vô hại: tệ nhất là dư 1 file mồ côi phía MiniMax.
            def _do_upload():
                with open(audio_path, 'rb') as f:
                    files = {
                        'file': (os.path.basename(audio_path), f, 'audio/mpeg'),
                        'purpose': (None, purpose)
                    }
                    return requests.post(url, headers=headers, files=files, timeout=30)

            logger.info(f"[Voice Clone] Uploading to: {url[:60]}...")
            response = self._post_with_retry(_do_upload)
            
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
        prompt_file_id: Optional[str] = None,
        prompt_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Clone voice from uploaded audio.
        
        Args:
            file_id: ID of uploaded source audio
            voice_name: Name for the cloned voice
            voice_id: Custom voice ID (optional, will be generated if not provided)
            prompt_file_id: Optional ID of uploaded prompt audio for better quality
            prompt_text: Optional text of prompt audio
            
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
                    "prompt_text": (prompt_text or "Sample prompt for voice cloning.").strip()
                }
            
            url = self._build_url("/voice_clone")
            
            # Build headers
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            logger.info(f"[Voice Clone] Calling: {url[:60]}...")

            # Make request — retry khi lỗi tầng mạng, giống upload_audio ở trên.
            response = self._post_with_retry(
                lambda: requests.post(url, headers=headers, json=payload, timeout=30)
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
                raise Exception(minimax_error_message(status_code, status_msg))
            
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
        prompt_audio_path: Optional[str] = None,
        prompt_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Complete workflow: Upload audio + Clone voice.
        
        Args:
            audio_path: Path to source audio file (10s - 5min, mp3/m4a/wav, max 20MB)
            voice_name: Name for the cloned voice
            voice_id: Custom voice ID (optional)
            prompt_audio_path: Optional path to prompt audio (<8s) for better quality
            prompt_text: Optional text of prompt audio to align and enhance similarity
            
        Returns:
            Dictionary with voice_id and other info
        """
        trimmed_prompt_path = None
        try:
            logger.info(f"[Voice Clone] Starting voice clone workflow...")
            logger.info(f"[Voice Clone] Audio: {audio_path}")
            logger.info(f"[Voice Clone] Name: {voice_name}")
            if prompt_text:
                logger.info(f"[Voice Clone] Prompt text length: {len(prompt_text)} chars")
            
            # Step 1: Upload source audio
            file_id = self.upload_audio(audio_path, purpose="voice_clone")
            
            # Step 2: Upload prompt audio (optional)
            prompt_file_id = None
            if prompt_audio_path:
                logger.info(f"[Voice Clone] Uploading prompt audio: {prompt_audio_path}")
                prompt_file_id = self.upload_audio(prompt_audio_path, purpose="prompt")
            else:
                # Tự động trích xuất 7.5s audio từ file mẫu để khớp âm vị (audio-phoneme alignment)
                try:
                    import subprocess
                    import uuid
                    import tempfile
                    import imageio_ffmpeg
                    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                    temp_dir = tempfile.gettempdir()
                    trimmed_prompt_path = os.path.join(temp_dir, f"prompt_{uuid.uuid4().hex}.mp3")
                    cmd = [
                        ffmpeg_exe, "-y", "-i", audio_path,
                        "-t", "7.5", "-ac", "1", "-ar", "32000",
                        trimmed_prompt_path
                    ]
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                    if os.path.exists(trimmed_prompt_path) and os.path.getsize(trimmed_prompt_path) > 0:
                        logger.info(f"[Voice Clone] Created 7.5s prompt audio ({os.path.getsize(trimmed_prompt_path)} bytes)")
                        
                        # Nếu người dùng không nhập prompt_text, tự động dùng OpenAI Whisper nhận diện chữ từ đoạn audio
                        if not prompt_text or not prompt_text.strip():
                            try:
                                import openai
                                openai_key = os.getenv("OPENAI_API_KEY")
                                if openai_key:
                                    client = openai.OpenAI(api_key=openai_key)
                                    with open(trimmed_prompt_path, "rb") as audio_f:
                                        transcription = client.audio.transcriptions.create(
                                            model="whisper-1",
                                            file=audio_f,
                                            language="vi"
                                        )
                                        if transcription and transcription.text:
                                            prompt_text = transcription.text.strip()
                                            logger.info(f"[Voice Clone] Auto-transcribed prompt_text via Whisper: {prompt_text}")
                            except Exception as transcribe_err:
                                logger.warning(f"[Voice Clone] Whisper auto-transcribe fallback: {transcribe_err}")

                        # Nếu có prompt_text (từ người dùng nhập hoặc Whisper tự nhận dạng), upload prompt audio
                        if prompt_text and prompt_text.strip():
                            prompt_file_id = self.upload_audio(trimmed_prompt_path, purpose="prompt")
                except Exception as trim_err:
                    logger.warning(f"[Voice Clone] Auto-trim prompt audio fallback: {trim_err}")
            
            # Step 3: Clone voice
            result = self.clone_voice(
                file_id=file_id,
                voice_name=voice_name,
                voice_id=voice_id,
                prompt_file_id=prompt_file_id,
                prompt_text=prompt_text,
            )
            
            logger.info(f"[Voice Clone] Workflow complete! voice_id: {result['voice_id']}")
            return result
            
        except Exception as e:
            logger.error(f"[Voice Clone] Workflow error: {str(e)}", exc_info=True)
            raise
        finally:
            if trimmed_prompt_path and os.path.exists(trimmed_prompt_path):
                try:
                    os.remove(trimmed_prompt_path)
                except Exception:
                    pass

    def delete_voice(self, voice_id: str) -> Dict[str, Any]:
        """
        Xoá hẳn một giọng đã clone khỏi tài khoản MiniMax (giải phóng slot giọng).

        API: POST /v1/delete_voice, body {"voice_type": "voice_cloning", "voice_id": ...}
        https://platform.minimax.io/docs/api-reference/voice-cloning-delete

        Trả về {'deleted': True} khi MiniMax xác nhận đã xoá, hoặc
        {'deleted': False, 'reason': ...} khi giọng KHÔNG còn tồn tại phía MiniMax
        (đã bị xoá tay, hoặc hết hạn 168h). Không tồn tại thì không phải lỗi —
        người dùng vẫn cần dọn bản ghi trong DB của mình, xem voice_views.delete_voice_api.
        Mọi lỗi khác (mạng, key sai, quyền) đều raise để tầng trên chặn việc xoá DB.
        """
        url = self._build_url("/delete_voice")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"voice_type": "voice_cloning", "voice_id": voice_id}

        logger.info(f"[Voice Clone] Deleting voice: {voice_id}")
        # Ít lần thử hơn clone: xoá chạy đồng bộ trong request của người dùng, không
        # phải background thread — chờ 10 x 30s sẽ làm treo cả BE lẫn trình duyệt.
        response = self._post_with_retry(
            lambda: requests.post(url, headers=headers, json=payload, timeout=30),
            max_attempts=3,
        )

        if response.status_code != 200:
            logger.error(f"[Voice Clone] Delete failed: {response.text}")
            raise Exception(f"Delete failed ({response.status_code}): {response.text}")

        data = response.json()
        base_resp = data.get('base_resp', {}) or {}
        status_code = base_resp.get('status_code', -1)
        status_msg = base_resp.get('status_msg', '')

        if status_code != 0:
            # 2013 = "invalid params, voice_id does not exist" — đo thật ngày 2026-08-06 bằng
            # cách gọi delete_voice trên một voice_id vừa bị xoá. Đừng nhầm với 2054
            # ("voice id not exist") mà endpoint TTS trả về: hai endpoint dùng hai mã khác nhau
            # cho cùng một tình huống.
            if status_code == 2013:
                logger.warning(f"[Voice Clone] Voice {voice_id} không còn trên MiniMax: {status_msg}")
                return {'deleted': False, 'reason': status_msg or 'voice not found'}
            raise Exception(minimax_error_message(status_code, status_msg))

        logger.info(f"[Voice Clone] Delete success! voice_id: {voice_id}")
        return {'deleted': True}


# Singleton instance
_voice_clone_service = None


def get_voice_clone_service(api_key: Optional[str] = None) -> MinimaxVoiceCloneService:
    """
    Get or create Voice Clone service instance.

    api_key: key do BE gửi kèm từng request qua header X-Minimax-Key — key MiniMax
    lưu ở .env của BE, không còn lưu ở .env AI. Có api_key thì tạo instance riêng
    (không cache vào singleton để key của request này không rò sang request khác);
    singleton + env chỉ còn là fallback cho management command chạy tay (clone_koc_voice).
    """
    global _voice_clone_service
    if api_key:
        return MinimaxVoiceCloneService(api_key=api_key)
    if _voice_clone_service is None:
        _voice_clone_service = MinimaxVoiceCloneService()
    return _voice_clone_service
