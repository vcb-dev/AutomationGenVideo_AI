"""Gemini xem + nghe video rồi viết lại thành kịch bản tiếng Việt — MỘT lần gọi.

Vì sao một lần gọi thay vì "bóc lời thoại" rồi "viết kịch bản" hai bước: Gemini nhận video nên
vừa nghe tiếng vừa xem hình. Video trang sức nhiều khi ý nằm ở hình (cận cảnh viên đá, thao
tác chế tác) chứ không ở lời nói — kịch bản viết khi nhìn được hình sát video hơn. Một lần gọi
cũng rẻ hơn và ít chỗ hỏng hơn.

Không có video (tải hỏng) thì viết từ tiêu đề + mô tả, đánh dấu has_voice=false.

Chỉ chạy khi VIDEO_SCRIPT_ENGINE=gemini (xem video_script_pipeline.script_engine).
"""

import json
import logging
import os
import re
import time
from typing import Optional

from django.conf import settings

from . import usage_meter
from .gemini_prices import AUDIO_TOKENS_PER_SECOND, cost_usd

logger = logging.getLogger(__name__)

FILE_PROCESSING_TIMEOUT = 120   # giây chờ Gemini xử lý file (PROCESSING → ACTIVE)
# Model mặc định: gemini-3.1-flash-lite — đo thật 2026-09-28 (video 76s, 1 lượt, xem + nghe video,
# viết lại kịch bản): ≈71đ/video, rẻ ~3 lần gemini-3.8-flash và ~2 lần quy trình tách
# gemini-3.5-transcribe + dịch, chất lượng kịch bản đạt. Đổi bằng VIDEO_SCRIPT_GEMINI_MODEL.
# KHÔNG đọc GEMINI_MODEL: biến đó dùng chung với transcribe cũ (đang là gemini-3.8-flash, đắt hơn).
DEFAULT_MODEL = 'gemini-3.1-flash-lite'
# Model cấu hình không tồn tại / bị Google ngừng (404 "not found" / "no longer available" — đã gặp
# thật với gemini-2.0-flash, và "gemini-3.1-flash" không tồn tại) → thử lại MỘT lần với tên luôn trỏ
# bản flash-LITE mới nhất, để gõ sai tên cũng không âm thầm chuyển sang model đắt hơn.
FALLBACK_MODEL = 'gemini-flash-lite-latest'
# 429 (vượt hạn mức, vd gói miễn phí chỉ 5 lượt/phút/model): chờ đúng số giây Google yêu cầu rồi
# thử lại MỘT lần — việc này chạy nền nên chờ được. Quá mức chờ này thì báo lỗi luôn.
MAX_RATE_LIMIT_WAIT = 90
GENERATE_TIMEOUT = 180          # giây cho lệnh sinh kịch bản

SCRIPT_RULES = """Viết lại thành KỊCH BẢN VIDEO NGẮN MỚI bằng tiếng Việt để team quay lại theo phong cách riêng:
- Giữ ý chính, thông tin sản phẩm và điểm hấp dẫn của video gốc; không dịch từng câu.
- Cấu trúc 3 phần: "hook" (1-2 câu mở đầu giữ chân người xem trong 3 giây đầu), "body" (nội dung chính, chia cảnh/câu thoại rõ ràng), "cta" (lời kêu gọi hành động cuối video).
- Văn phong tự nhiên như người Việt nói, câu ngắn, dễ đọc thành lời.
- Không bịa thông số/giá không có trong video."""

VIDEO_PROMPT = f"""Bạn là biên kịch video ngắn cho một thương hiệu trang sức Việt Nam.
Hãy XEM và NGHE video đính kèm.

Tiêu đề gốc: {{title}}
Mô tả gốc: {{description}}

Việc 1 — Lời thoại: chép lại nguyên văn lời nói trong video (giữ ngôn ngữ gốc). Nếu video không có lời nói (chỉ nhạc/âm thanh nền) thì để transcript là chuỗi rỗng và has_voice=false.
Việc 2 — Kịch bản: {SCRIPT_RULES}
Nếu không có lời thoại, dựa vào hình ảnh trong video + tiêu đề/mô tả để viết.

Trả về DUY NHẤT một JSON đúng dạng:
{{{{"has_voice": true/false, "language": "mã ngôn ngữ lời thoại, vd zh/vi/en, rỗng nếu không có", "transcript": "...", "script": {{{{"hook": "...", "body": "...", "cta": "..."}}}}}}}}"""

TEXT_PROMPT = f"""Bạn là biên kịch video ngắn cho một thương hiệu trang sức Việt Nam.
Không xem được video gốc, chỉ có:
Tiêu đề gốc: {{title}}
Mô tả gốc: {{description}}

{SCRIPT_RULES}

Trả về DUY NHẤT một JSON đúng dạng:
{{{{"script": {{{{"hook": "...", "body": "...", "cta": "..."}}}}}}}}"""


class GeminiNotConfigured(Exception):
    pass


class GeminiQuotaExceeded(Exception):
    """Hết hạn mức Gemini — thông điệp tiếng Việt ngắn gọn để hiện thẳng trên thẻ Content."""


def _quota_message(err: Exception) -> str:
    msg = str(err)
    free = 'free_tier' in msg.lower() or 'FreeTier' in msg
    if 'PerDay' in msg:
        return ('Gemini hết hạn mức trong ngày' + (' (gói miễn phí)' if free else '')
                + ' — bật billing cho project của VIDEO_TO_TEXT_GEMINI_API_KEY hoặc thử lại vào ngày mai.')
    return 'Gemini đang vượt hạn mức số lượt/phút' + (' (gói miễn phí: 5 lượt/phút)' if free else '') + ' — thử lại sau ít phút.'


def _api_key() -> str:
    """Khoá Gemini của module chuyển video thành text — tách khỏi GEMINI_API_KEY của module tạo ảnh
    (ảnh thẻ), không lấy tạm khoá đó khi thiếu."""
    key = (getattr(settings, 'VIDEO_TO_TEXT_GEMINI_API_KEY', '') or os.getenv('VIDEO_TO_TEXT_GEMINI_API_KEY', '')).strip()
    if not key:
        raise GeminiNotConfigured('Chưa cấu hình VIDEO_TO_TEXT_GEMINI_API_KEY trên AI Service.')
    return key


def _model_name() -> str:
    return str(getattr(settings, 'VIDEO_SCRIPT_GEMINI_MODEL', '')
               or os.getenv('VIDEO_SCRIPT_GEMINI_MODEL', '') or DEFAULT_MODEL).strip()


def _model_gone(err: Exception) -> bool:
    msg = str(err).lower()
    return '404' in msg and ('no longer available' in msg or 'not found' in msg)


def _retry_delay(err: Exception) -> float:
    """Số giây Google yêu cầu chờ trong lỗi 429 (0 nếu không phải 429 / không đọc được)."""
    msg = str(err)
    if '429' not in msg:
        return 0.0
    # Hết hạn mức THEO NGÀY: chờ vài chục giây không giải quyết được gì (phải sang ngày mới).
    if 'PerDay' in msg:
        return 0.0
    m = re.search(r'retry in ([\d.]+)s', msg) or re.search(r'seconds:\s*(\d+)', msg)
    return float(m.group(1)) if m else 30.0


def _is_transient(err: Exception) -> bool:
    """Lỗi tạm thời phía Google — 504 hết giờ, 503 quá tải, 500 — thử lại thường qua.
    Gặp khi test thật 2026-09-28: một lượt treo 180s rồi 504 trong khi lượt song song vẫn chạy."""
    try:
        from google.api_core import exceptions as gexc
        if isinstance(err, (gexc.DeadlineExceeded, gexc.ServiceUnavailable, gexc.InternalServerError)):
            return True
    except ImportError:
        pass
    msg = str(err)
    return msg.startswith(('504', '503', '500 ')) or 'Deadline Exceeded' in msg


def _generate(genai, contents, timeout: int, audio_seconds: float = 0):
    """_generate_once với hai kiểu thử lại MỘT lần: lỗi tạm thời phía Google (504/503/500) thì thử
    lại ngay; vượt hạn mức (429) thì chờ theo yêu cầu của Google rồi thử lại. Vẫn hết hạn mức →
    GeminiQuotaExceeded với thông điệp tiếng Việt."""
    try:
        return _generate_once(genai, contents, timeout, audio_seconds)
    except Exception as e:  # noqa: BLE001
        if _is_transient(e):
            logger.warning(f'[VideoScript] Gemini lỗi tạm thời ({str(e)[:80]}) — thử lại một lần.')
            return _generate_once(genai, contents, timeout, audio_seconds)
        if '429' not in str(e):
            raise
        wait = _retry_delay(e)
        if not wait or wait > MAX_RATE_LIMIT_WAIT:
            raise GeminiQuotaExceeded(_quota_message(e)) from e
        logger.warning(f'[VideoScript] Gemini vượt hạn mức (429) — chờ {wait:.0f}s rồi thử lại. '
                       'Gói miễn phí chỉ 5 lượt/phút/model: nên bật billing cho project của VIDEO_TO_TEXT_GEMINI_API_KEY.')
        time.sleep(wait + 1)
        try:
            return _generate_once(genai, contents, timeout, audio_seconds)
        except Exception as e2:  # noqa: BLE001
            if '429' in str(e2):
                raise GeminiQuotaExceeded(_quota_message(e2)) from e2
            raise


def _record_usage(model: str, response, audio_seconds: float = 0) -> None:
    """Ghi số token THẬT Gemini tính cho lượt này (usage_metadata) để BE thống kê chi phí.
    Output lấy total - prompt để gồm cả token "suy nghĩ" (tính tiền theo giá output)."""
    meta = getattr(response, 'usage_metadata', None)
    if meta is None:
        return
    try:
        prompt = int(getattr(meta, 'prompt_token_count', 0) or 0)
        total = int(getattr(meta, 'total_token_count', 0) or 0)
        output = total - prompt if total >= prompt and total else int(getattr(meta, 'candidates_token_count', 0) or 0)
        audio_tokens = round(AUDIO_TOKENS_PER_SECOND * max(0.0, audio_seconds or 0))
        cost = cost_usd(model, prompt, output, audio_tokens=audio_tokens)
    except (TypeError, ValueError) as e:
        # Gemini đã trả lời (đã tính tiền) — lỗi thống kê không được làm mất kịch bản
        logger.warning('Không tính được chi phí Gemini (%s): %s', model, e)
        return
    usage_meter.record('gemini', model, cost_usd=cost, status=200, input_tokens=prompt, output_tokens=output)


def _generate_once(genai, contents, timeout: int, audio_seconds: float = 0):
    """generate_content với model cấu hình; model đã bị Google ngừng thì thử FALLBACK_MODEL."""
    config = {'response_mime_type': 'application/json', 'temperature': 0.7}
    name = _model_name()
    try:
        response = genai.GenerativeModel(name, generation_config=config).generate_content(
            contents, request_options={'timeout': timeout})
    except Exception as e:  # noqa: BLE001
        if not _model_gone(e) or name == FALLBACK_MODEL:
            raise
        logger.warning(f'[VideoScript] model {name} không dùng được (không tồn tại / đã bị ngừng) — dùng tạm '
                       f'{FALLBACK_MODEL}. Sửa VIDEO_SCRIPT_GEMINI_MODEL cho đúng. Lỗi gốc: {str(e)[:160]}')
        name = FALLBACK_MODEL
        response = genai.GenerativeModel(name, generation_config=config).generate_content(
            contents, request_options={'timeout': timeout})
    _record_usage(name, response, audio_seconds)
    return response


def _parse_json(text: str) -> dict:
    text = (text or '').strip()
    if text.startswith('```'):
        text = text.strip('`')
        text = text[text.find('{'):]
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        raise ValueError('Gemini không trả JSON')
    return json.loads(text[start:end + 1])


def format_script(script: dict) -> str:
    """Ghép 3 phần thành văn bản để lưu vào ApprovedContent.script."""
    script = script or {}
    parts = [
        ('HOOK', script.get('hook')),
        ('NỘI DUNG', script.get('body')),
        ('KÊU GỌI HÀNH ĐỘNG', script.get('cta')),
    ]
    return '\n\n'.join(f'【{label}】\n{str(text).strip()}' for label, text in parts if text and str(text).strip())


def generate_script(video_path: Optional[str], title: str, description: str, audio_seconds: float = 0) -> dict:
    """Trả {has_voice, language, transcript, script_text, source}. Ném lỗi nếu Gemini hỏng.
    `audio_seconds`: độ dài phần tiếng của video — để tính đúng giá âm thanh khi ghi chi phí."""
    import google.generativeai as genai

    genai.configure(api_key=_api_key())
    fields = {'title': (title or '').strip()[:500] or '(không có)', 'description': (description or '').strip()[:2000] or '(không có)'}

    if not video_path:
        response = _generate(genai, TEXT_PROMPT.format(**fields), GENERATE_TIMEOUT)
        data = _parse_json(response.text)
        return {'has_voice': False, 'language': '', 'transcript': '',
                'script_text': format_script(data.get('script')), 'source': 'gemini_text'}

    gemini_file = None
    try:
        t0 = time.time()
        gemini_file = genai.upload_file(video_path)
        started = time.time()
        while gemini_file.state.name == 'PROCESSING':
            if time.time() - started > FILE_PROCESSING_TIMEOUT:
                raise TimeoutError('Gemini xử lý video quá lâu.')
            time.sleep(2)
            gemini_file = genai.get_file(gemini_file.name)
        if gemini_file.state.name == 'FAILED':
            raise RuntimeError('Gemini không xử lý được file video.')
        t_ready = time.time()
        response = _generate(genai, [gemini_file, VIDEO_PROMPT.format(**fields)], GENERATE_TIMEOUT, audio_seconds)
        # Thời gian từng chặng — để biết chậm ở đâu khi kịch bản ra lâu (test thật 2026-09-28 có lượt ~6 phút)
        logger.info(f'[VideoScript] Gemini {_model_name()}: tải lên {started - t0:.1f}s · chờ xử lý file '
                    f'{t_ready - started:.1f}s · sinh kịch bản {time.time() - t_ready:.1f}s')
        data = _parse_json(response.text)
        transcript = str(data.get('transcript') or '').strip()
        return {
            'has_voice': bool(data.get('has_voice')) and bool(transcript),
            'language': str(data.get('language') or '').strip(),
            'transcript': transcript,
            'script_text': format_script(data.get('script')),
            'source': 'gemini_video',
        }
    finally:
        if gemini_file:
            try:
                genai.delete_file(gemini_file.name)
            except Exception as e:  # noqa: BLE001
                logger.warning(f'[VideoScript] không xoá được file trên Gemini {gemini_file.name}: {e}')
