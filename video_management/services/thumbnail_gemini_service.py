"""Gemini enhance background cho thumbnail — chỉ xử lý nền, không vẽ person/chữ."""
from __future__ import annotations

import io
import logging
import os
from typing import Tuple

from django.conf import settings
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'gemini-2.0-flash-preview-image-generation'
DEFAULT_TIMEOUT_S = 45


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

# Gemini prompt
def _build_prompt(size: Tuple[int, int], orientation: str) -> str:
    w, h = size
    return (
        'Enhance this image as a professional YouTube/TikTok thumbnail BACKGROUND only.\n'
        f'- Target aspect ratio: {w}x{h} ({orientation})\n'
        '- Cinematic lighting, vibrant but natural colors\n'
        '- Subtle depth-of-field blur on background\n'
        '- Keep bottom 40% relatively clear (a person PNG will be overlaid)\n'
        '- Darken top 30% slightly (white title text will be placed there)\n'
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