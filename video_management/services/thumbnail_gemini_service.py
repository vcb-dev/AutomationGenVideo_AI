"""Gemini enhance background cho thumbnail — chỉ xử lý nền, không vẽ person/chữ."""
from __future__ import annotations

import io
import logging
import os
from typing import Tuple

from django.conf import settings
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'gemini-2.5-flash-image'
DEFAULT_TIMEOUT_S = 45
MAX_CONTENT_PROMPT_LEN = 2000
MAX_REFERENCE_IMAGES = 3
# Kích thước theo orientation — mọi template dùng chung
ORIENTATION_SIZES: dict[str, tuple[int, int]] = {
    'landscape': (1280, 720),
    'portrait': (720, 1280),
}


class ThumbnailGeminiError(Exception):
    """Lỗi có thể fallback sang PIL crop."""

# Gemini API key
def _gemini_api_key() -> str:
    key = (getattr(settings, 'GEMINI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '')).strip()
    if not key or key.startswith('your_'):
        raise ThumbnailGeminiError('Chưa cấu hình GEMINI_API_KEY')
    return key

# Gemini model name
def _gemini_model_name() -> str:
    return (
        os.getenv('GEMINI_THUMBNAIL_MODEL')
        or getattr(settings, 'GEMINI_THUMBNAIL_MODEL', None)
        or DEFAULT_MODEL
    ).strip()

def _detect_mime(data: bytes) -> str:
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'

def _build_generate_content_prompt(
    user_prompt: str,
    size: tuple[int, int],
    orientation: str,
    has_references: bool,
) -> str:
    w, h = size
    ref_hint = (
        'Use the attached reference image(s) for style, color palette, subject, or composition guidance.\n'
        if has_references
        else ''
    )
    return (
        'Generate a professional YouTube/TikTok/Facebook thumbnail BACKGROUND image.\n'
        f'- Target aspect ratio: {w}x{h} ({orientation})\n'
        f'{ref_hint}'
        '- Cinematic lighting, vibrant but natural colors\n'
        '- Do NOT add any text, people, faces, logos, or watermarks\n'
        '- Output a single full-frame background image\n'
        f'User request:\n{user_prompt.strip()}'
    )

def generate_content_with_gemini(
    prompt: str,
    orientation: str,
    reference_images: list[bytes] | None = None,
    template: str = 'simple_v1',
) -> Image.Image:
    """
    Tạo ảnh nội dung từ prompt (+ ảnh tham chiếu optional).
    Trả PIL Image RGB đúng kích thước template.
    """
    import google.generativeai as genai
    from google.api_core import exceptions as google_api_exceptions
    user_prompt = (prompt or '').strip()
    if not user_prompt:
        raise ThumbnailGeminiError('Vui lòng nhập prompt mô tả ảnh nội dung')
    if len(user_prompt) > MAX_CONTENT_PROMPT_LEN:
        raise ThumbnailGeminiError(f'Prompt tối đa {MAX_CONTENT_PROMPT_LEN} ký tự')
    orientation = (orientation or 'landscape').strip().lower()
    if orientation not in ('landscape', 'portrait'):
        raise ThumbnailGeminiError('Layout phải là landscape hoặc portrait')
    if orientation not in ORIENTATION_SIZES:
        raise ThumbnailGeminiError(f'Layout không hỗ trợ: {orientation}')
    size = ORIENTATION_SIZES[orientation]
    refs = reference_images or []
    if len(refs) > MAX_REFERENCE_IMAGES:
        raise ThumbnailGeminiError(f'Tối đa {MAX_REFERENCE_IMAGES} ảnh tham chiếu')
    api_key = _gemini_api_key()
    model_name = _gemini_model_name()
    timeout_s = int(os.getenv('GEMINI_THUMBNAIL_TIMEOUT', DEFAULT_TIMEOUT_S))
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)
    system_prompt = _build_generate_content_prompt(user_prompt, size, orientation, bool(refs))
    parts: list = [system_prompt]
    for ref_bytes in refs:
        parts.append({'mime_type': _detect_mime(ref_bytes), 'data': ref_bytes})
    logger.info(
        '[THUMBNAIL-GEMINI] generate-content model=%s size=%s refs=%s',
        model_name, size, len(refs),
    )
    try:
        response = model.generate_content(
            parts,
            request_options={'timeout': timeout_s},
        )
    except (google_api_exceptions.DeadlineExceeded, google_api_exceptions.RetryError) as exc:
        raise ThumbnailGeminiError(f'Gemini timeout ({timeout_s}s)') from exc
    except Exception as exc:
        raise ThumbnailGeminiError(f'Gemini request failed: {exc}') from exc
    raw_bytes = _extract_image_bytes(response)
    img = Image.open(io.BytesIO(raw_bytes))
    return _fit_to_size(img, size)

# Gemini prompt
def _build_prompt(size: Tuple[int, int], orientation: str) -> str:
    w, h = size
    return (
        'Enhance this image as a professional YouTube/TikTok/Facebook thumbnail BACKGROUND only.\n'
        f'- Target aspect ratio: {w}x{h} ({orientation})\n'
        '- Cinematic lighting, vibrant but natural colors\n'
        '- Subtle depth-of-field blur on background\n'
        '- Keep bottom 40% relatively clear (a person PNG will be overlaid)\n'
        '- Keep natural brightness and colors — do NOT darken or add vignette\n'
        '- Preserve original lighting — text contrast will be handled separately\n'
        '- Do NOT add any text, people, logos, or watermarks\n'
        '- Preserve the main subject and mood of the original photo\n'
        '- Output a single full-frame background image'
    )

# Gemini extract image bytes: lấy bytes ảnh đầu tiên từ Gemini response.
def _extract_image_bytes(response) -> bytes:
    """Lấy bytes ảnh đầu tiên từ Gemini response."""
    candidates = getattr(response, 'candidates', None) or []
    for candidate in candidates:
        content = getattr(candidate, 'content', None)
        if not content:
            continue
        for part in getattr(content, 'parts', []) or []:
            inline = getattr(part, 'inline_data', None)
            if inline and getattr(inline, 'data', None):
                mime = (getattr(inline, 'mime_type', '') or '').lower()
                if mime.startswith('image/'):
                    return inline.data
    raise ThumbnailGeminiError('Gemini không trả ảnh trong response')

# Gemini fit to size: resize/crop về đúng kích thước template.
def _fit_to_size(img: Image.Image, size: Tuple[int, int]) -> Image.Image:
    """Resize/crop về đúng kích thước template."""
    return ImageOps.fit(
        img.convert('RGB'),
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )

# Gemini enhance background with Gemini model.
def enhance_background_with_gemini(
    content_image_bytes: bytes,
    size: Tuple[int, int],
    orientation: str,
) -> Image.Image:
    """
    Gọi Gemini enhance nền từ ảnh nội dung.
    Trả PIL Image RGB đúng `size`.
    Raise ThumbnailGeminiError nếu fail — caller fallback PIL.
    """
    import google.generativeai as genai
    from google.api_core import exceptions as google_api_exceptions

    api_key = _gemini_api_key()
    model_name = _gemini_model_name()
    timeout_s = int(os.getenv('GEMINI_THUMBNAIL_TIMEOUT', DEFAULT_TIMEOUT_S))

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)
    prompt = _build_prompt(size, orientation)

    # Detect mime từ magic bytes
    mime = 'image/jpeg'
    if content_image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        mime = 'image/png'
    elif content_image_bytes[:4] == b'RIFF' and content_image_bytes[8:12] == b'WEBP':
        mime = 'image/webp'

    logger.info('[THUMBNAIL-GEMINI] model=%s size=%s orientation=%s', model_name, size, orientation)

    try:
        response = model.generate_content(
            [
                prompt,
                {'mime_type': mime, 'data': content_image_bytes},
            ],
            request_options={'timeout': timeout_s},
        )
    except (google_api_exceptions.DeadlineExceeded, google_api_exceptions.RetryError) as exc:
        raise ThumbnailGeminiError(f'Gemini timeout ({timeout_s}s)') from exc
    except Exception as exc:
        raise ThumbnailGeminiError(f'Gemini request failed: {exc}') from exc

    raw_bytes = _extract_image_bytes(response)
    img = Image.open(io.BytesIO(raw_bytes))
    return _fit_to_size(img, size)