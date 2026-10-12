"""
Minimax TTS Service - Text-to-Speech using Minimax AI.

API Docs: https://platform.minimax.io/docs/api-reference/text-to-speech-t2a-v2
"""

import logging
import os
import re
import time
from typing import Optional, Dict, Any

from django.conf import settings
import requests

from .minimax_errors import minimax_error_from_response

logger = logging.getLogger(__name__)


def split_sentences_for_tts(text: str) -> str:
    """
    Chuẩn hoá văn bản trước khi gửi tới MiniMax TTS:
    Tách các câu và vế câu thành các dòng riêng biệt (\\n) theo dấu chấm, chấm hỏi,
    chấm than, dấu phẩy, hai chấm, chấm phẩy, gạch ngang.
    MiniMax API chỉ phân tách phụ đề (subtitle) theo từng dòng (\\n).
    Tách theo cả dấu phẩy giúp phụ đề hiển thị ngắn gọn, nhịp nhàng từng vế câu
    chuẩn theo từng giây ngắt nghỉ của giọng đọc (tối ưu cho video ngắn / TikTok / Reels).
    """
    if not text or not isinstance(text, str):
        return text

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result_lines = []
    for line in lines:
        # Chuẩn hoá khoảng trắng trước dấu câu: ' ,' -> ','
        cleaned = re.sub(r'\s+([.!?;:,—–])', r'\1', line)
        # Tách sau dấu câu (. ! ? : ; — –) và dấu phẩy (,) khi có khoảng trắng theo sau và không phải giữa các chữ số
        parts = re.split(r'(?<=[.!?:;—–])\s+(?=[^\s])|(?<=,)\s+(?=[^\s\d])', cleaned)
        for p in parts:
            p_strip = p.strip()
            if p_strip:
                result_lines.append(p_strip)

    return '\n'.join(result_lines)




def format_srt_timestamp(ms: float) -> str:
    """Chuyển milliseconds thành định dạng chuẩn timestamp của SRT: HH:MM:SS,mmm"""
    total_ms = max(0, int(round(ms)))
    hours = total_ms // 3600000
    mins = (total_ms % 3600000) // 60000
    secs = (total_ms % 60000) // 1000
    millis = total_ms % 1000
    return f"{hours:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def normalize_subtitle_entry(entry: dict) -> Optional[dict]:
    """Chuẩn hoá 1 mục subtitle từ nhiều kiểu tên field của MiniMax sang {text, start_ms, end_ms}"""
    if not isinstance(entry, dict):
        return None

    text = (entry.get('text') or entry.get('content') or entry.get('word') or '').strip()
    if not text:
        return None

    # Tìm thời gian bắt đầu (hỗ trợ nhiều biến thể theo format API và extension cũ)
    start_val = None
    for k in ('time_begin', 'start_time', 'begin_time', 'start', 'begin'):
        if k in entry and entry[k] is not None:
            start_val = entry[k]
            break

    # Tìm thời gian kết thúc
    end_val = None
    for k in ('time_end', 'end_time', 'finish_time', 'end', 'finish'):
        if k in entry and entry[k] is not None:
            end_val = entry[k]
            break

    if start_val is None or end_val is None:
        return None

    try:
        start_ms = float(start_val)
        end_ms = float(end_val)
    except (ValueError, TypeError):
        return None

    if end_ms < start_ms:
        end_ms = start_ms

    return {
        'text': text,
        'start_ms': start_ms,
        'end_ms': end_ms,
    }


def subtitles_to_srt(subtitles_list: list) -> str:
    """Chuyển danh sách các mục subtitle thành chuỗi chuẩn SRT."""
    if not subtitles_list or not isinstance(subtitles_list, list):
        return ""

    normalized = []
    for item in subtitles_list:
        norm = normalize_subtitle_entry(item)
        if norm:
            normalized.append(norm)

    if not normalized:
        return ""

    # Sắp xếp theo thời gian bắt đầu
    normalized.sort(key=lambda s: s['start_ms'])

    blocks = []
    for idx, item in enumerate(normalized, start=1):
        start_ts = format_srt_timestamp(item['start_ms'])
        end_ts = format_srt_timestamp(item['end_ms'])
        blocks.append(f"{idx}\n{start_ts} --> {end_ts}\n{item['text']}\n")

    return "\n".join(blocks).strip() + "\n"


class MinimaxTTSService:
    """
    Service for generating audio using Minimax TTS API.
    """
    
    # Retry mạng: kết nối tới api.minimax.io chập chờn ở cả 2 IP load-balancer (đo
    # 2026-07-07). 5 x 45s = ~225s, cố ý dưới trần chờ 300s của BE. TTS thật chỉ mất
    # 3-10s nên 45s/lần là quá đủ. ĐỌC TỪ ĐÂY, đừng gõ lại số vào chuỗi log.
    MAX_ATTEMPTS = 5
    ATTEMPT_TIMEOUT_S = 45
    # Tải file audio khi MiniMax trả URL thay vì bytes — file mp3 vài MB.
    DOWNLOAD_TIMEOUT_S = 120

    # Chất lượng cao nhất cho MP3 của MiniMax: 44.1kHz / 256kbps / mono.
    # Hạ xuống chỉ nên làm khi có lý do đo được (dung lượng, băng thông) — giọng
    # clone nghe rõ sự khác biệt ở bitrate thấp.
    AUDIO_SETTING = {
        "sample_rate": 44100,
        "bitrate": 256000,
        "format": "mp3",
        "channel": 1,
    }

    def __init__(self, api_key: Optional[str] = None, group_id: Optional[str] = None):
        """
        Initialize Minimax TTS Service.
        
        Args:
            api_key: Minimax API Key (JWT token)
            group_id: Minimax Group ID
        """
        self.api_key = api_key or getattr(settings, 'MINIMAX_API_KEY', '')
        # Key kiểu mới "sk-api-..." tự gắn với group — KHÔNG cần GroupId; gửi kèm
        # GroupId của account khác sẽ lỗi 1004 "token not match group". Chỉ key JWT
        # kiểu cũ (eyJ...) mới cần. Vì vậy group_id là tùy chọn.
        self.group_id = group_id or getattr(settings, 'MINIMAX_GROUP_ID', '')
        self.model = getattr(settings, 'MINIMAX_TTS_MODEL', 'speech-2.8-hd')
        self.base_url = f"{getattr(settings, 'MINIMAX_API_BASE_URL', 'https://api.minimax.io/v1')}/t2a_v2"

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
        output_path: Optional[str] = None,
        model: Optional[str] = None,
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
            model: Optional model override (e.g. "speech-2.8-turbo", "speech-2.8-hd")

        Returns:
            Dictionary with:
            {
                'success': bool,
                'audio_url': str,          # URL to download audio
                'file_path': str,          # Local path if output_path provided
                'duration': float,         # Audio duration in seconds
                'extra_info': dict,
                'srt_content': Optional[str],
                'srt_file_path': Optional[str],
            }
        """
        try:
            chosen_model = model or self.model
            text_to_synthesize = split_sentences_for_tts(text)
            line_count = len(text_to_synthesize.splitlines())
            logger.info(f"🎤 Minimax TTS - Voice: {voice_id}, Model: {chosen_model}, Text: {len(text)} chars, Lines: {line_count}")
            
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
                "model": chosen_model,
                "text": text_to_synthesize,
                "stream": False,
                "voice_setting": voice_setting,
                "audio_setting": dict(self.AUDIO_SETTING),
                # Bật tạo phụ đề để xuất file SRT đồng bộ cùng voice
                "subtitle_enable": True,
                "subtitle_type": "sentence",
            }
            if language_boost:
                payload["language_boost"] = language_boost
            
            # Build headers
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            # Chỉ gắn GroupId khi có (key JWT cũ); key sk-api không cần
            url = f"{self.base_url}?GroupId={self.group_id}" if self.group_id else self.base_url
            
            logger.info(f"📡 Calling Minimax API: {url[:50]}...")

            # Retry khi lỗi tầng mạng — xem MAX_ATTEMPTS / ATTEMPT_TIMEOUT_S.
            start_time = time.time()
            last_network_error = None
            response = None
            for attempt in range(1, self.MAX_ATTEMPTS + 1):
                try:
                    response = requests.post(
                        url, headers=headers, json=payload, timeout=self.ATTEMPT_TIMEOUT_S
                    )
                    last_network_error = None
                    break
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as net_err:
                    last_network_error = net_err
                    logger.warning(
                        f"📡 Minimax TTS attempt {attempt}/{self.MAX_ATTEMPTS} failed (network): {net_err}"
                    )
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
                # Dict thô của MiniMax từng đi thẳng ra toast cho người bán hàng đọc. Lỗi hay gặp
                # nhất ở đây là giới hạn tài khoản (hết tiền/hết slot) — chuyện có người xử lý
                # được, nên phải nói thành câu họ hiểu.
                raise Exception(minimax_error_from_response(data))

            if audio_file.startswith('http'):
                audio_url = audio_file
                logger.info(f"✅ Audio URL: {audio_url[:100]}...")
                # Caller (voice_tts_api) serves the file from output_path via /media —
                # nếu không tải về thì URL /media trả 404. Download về output_path.
                if output_path:
                    dl = requests.get(audio_file, timeout=self.DOWNLOAD_TIMEOUT_S)
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
            
            # Trích xuất subtitle (phụ đề SRT) từ phản hồi của MiniMax
            srt_content = None
            srt_file_path = None
            raw_subtitles = None

            # 1. Kiểm tra mảng subtitles trực tiếp trong response hoặc extra_info
            if isinstance(data.get('subtitles'), list):
                raw_subtitles = data.get('subtitles')
            elif isinstance(inner.get('subtitles'), list):
                raw_subtitles = inner.get('subtitles')
            elif isinstance(extra_info.get('subtitles'), list):
                raw_subtitles = extra_info.get('subtitles')

            # 2. Nếu MiniMax trả về URL subtitle_file (link JSON chứa timestamps)
            subtitle_file_url = (
                data.get('subtitle_file')
                or inner.get('subtitle_file')
                or extra_info.get('subtitle_file')
            )
            if not raw_subtitles and subtitle_file_url and str(subtitle_file_url).startswith('http'):
                try:
                    logger.info(f"📥 Downloading subtitle_file from: {str(subtitle_file_url)[:80]}...")
                    sub_resp = requests.get(subtitle_file_url, timeout=15)
                    if sub_resp.status_code == 200:
                        sub_json = sub_resp.json()
                        if isinstance(sub_json, list):
                            raw_subtitles = sub_json
                        elif isinstance(sub_json, dict):
                            raw_subtitles = sub_json.get('subtitles') or sub_json.get('data') or sub_json.get('sentences')
                except Exception as sub_err:
                    logger.warning(f"⚠️ Failed to download/parse subtitle_file: {sub_err}")

            # 3. Tạo chuỗi SRT và lưu file nếu có dữ liệu
            if raw_subtitles and isinstance(raw_subtitles, list):
                srt_content = subtitles_to_srt(raw_subtitles)
                if srt_content and output_path:
                    # Ghi file .srt cùng tên với file .mp3
                    base_no_ext, _ = os.path.splitext(output_path)
                    srt_file_path = f"{base_no_ext}.srt"
                    try:
                        with open(srt_file_path, 'w', encoding='utf-8') as srt_f:
                            srt_f.write(srt_content)
                        logger.info(f"✅ Subtitle SRT saved to: {srt_file_path}")
                    except Exception as srt_write_err:
                        logger.warning(f"⚠️ Failed to write SRT file to disk: {srt_write_err}")

            # Get duration from extra_info
            duration = extra_info.get('audio_length', 0) or extra_info.get('duration', 0)
            
            result = {
                'success': True,
                'audio_url': audio_url,
                'file_path': output_path if output_path else None,
                'duration': duration,
                'extra_info': extra_info,
                'srt_content': srt_content,
                'srt_file_path': srt_file_path,
            }
            
            logger.info(f"✅ Minimax TTS Success - Duration: {duration}s, Subtitle: {'Yes' if srt_content else 'No'}")
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
