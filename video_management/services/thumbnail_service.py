"""
Template engine deterministic cho AI Thumbnail Generator.
Layout do dict TEMPLATES kiểm soát — Gemini KHÔNG tham gia bước composite cuối.
"""
from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Tuple

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

from video_management.utils.url_safety import is_public_http_url

logger = logging.getLogger(__name__)

Orientation = Literal['landscape', 'portrait']

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, 'assets', 'thumbnail')
PERSONS_DIR = os.path.join(ASSETS_DIR, 'persons')
FONTS_DIR = os.path.join(ASSETS_DIR, 'fonts')

MAX_TITLE_LEN = 200
MAX_CONTENT_BYTES = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 20
PERSON_SLUG_RE = re.compile(r'^[a-zA-Z0-9_-]+$')

# MVP: 1 template. Mở rộng sau bằng cách thêm key mới.
TEMPLATES: dict[str, dict[str, dict[str, Any]]] = {
    'simple_v1': {
        #landscape là hình ảnh ngang
        'landscape': {
            'size': (1280, 720),
            'content_bg': {'fit': 'cover', 'blur': 0},
            'overlay': {'color': (0, 0, 0, 90)},
            'person': {
                'anchor': 'bottom_center',
                'max_height_ratio': 0.62,
                'bottom_padding': 0,
            },
            'text': {
                'area': (60, 40, 1220, 260),
                'color_mode': 'auto',   # quan trọng — không fixed white
                'align': 'center',
                'max_lines': 3,
                'font_size_start': 72,
                'font_size_min': 28,
            },
        },
        #portrait là hình ảnh dọc
        'portrait': {
            'size': (720, 1280),
            'content_bg': {'fit': 'cover', 'blur': 0},
            'overlay': {'color': (0, 0, 0, 100)},
            'person': {
                'anchor': 'bottom_center',
                'max_height_ratio': 0.55,
                'bottom_padding': 0,
            },
            'text': {
                'area': (40, 80, 680, 340),
                'color_mode': 'auto',
                'align': 'center',
                'max_lines': 4,
                'font_size_start': 72,
                'font_size_min': 28,
            },
        },
    },
}

FONT_MAP: dict[str, str] = {
    'bevietnam_bold': 'BeVietnamPro-Bold.ttf',
}


@dataclass
class ThumbnailInput:
    title: str
    template: str
    orientation: Orientation
    font_key: str
    content_image_bytes: bytes
    person_image_bytes: bytes
    enhance_background: bool = False

# Danh sách các template và fonts
def list_templates() -> dict[str, list[dict[str, str | list[str]]]]:
    fonts = [
        {'id': key, 'label': key.replace('_', ' ').title()}
        for key, filename in FONT_MAP.items()
        if os.path.isfile(os.path.join(FONTS_DIR, filename))
    ]
    templates = [
        {
            'id': template_id,
            'label': template_id.replace('_', ' ').title(),
            'orientations': list(cfg.keys()),
        }
        for template_id, cfg in TEMPLATES.items()
    ]
    return {'templates': templates, 'fonts': fonts}

# Load font từ assets
def _load_font(font_key: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    filename = FONT_MAP.get(font_key) or FONT_MAP['bevietnam_bold']
    path = os.path.join(FONTS_DIR, filename)
    if os.path.isfile(path):
        return ImageFont.truetype(path, size=size)
    logger.warning('[THUMBNAIL] Font file missing: %s — fallback default', path)
    return ImageFont.load_default()

# Mở ảnh từ bytes và chuyển sang chế độ RGBA
def _open_rgba(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    return img.convert('RGBA')

# Fit ảnh vào kích thước
def _fit_cover(img: Image.Image, size: Tuple[int, int]) -> Image.Image:
    return ImageOps.fit(img, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))

# Paste người vào ảnh
def _paste_person(canvas: Image.Image, person: Image.Image, spec: dict[str, Any]) -> None:
    cw, ch = canvas.size
    max_h = int(ch * float(spec.get('max_height_ratio', 0.6)))
    ratio = max_h / max(person.height, 1)
    new_w = max(1, int(person.width * ratio))
    new_h = max(1, int(person.height * ratio))
    person = person.resize((new_w, new_h), Image.Resampling.LANCZOS)

    bottom_pad = int(spec.get('bottom_padding', 0))
    x = (cw - new_w) // 2
    y = ch - new_h - bottom_pad
    canvas.alpha_composite(person, (x, y))

# Wrap title là viết dài thành nhiều dòng
def _wrap_title(
    title: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    title = ' '.join((title or '').strip().split())
    if not title:
        return ['']

    words = title.split(' ')
    lines: list[str] = []
    current = ''

    for word in words:
        trial = f'{current} {word}'.strip()
        bbox = font.getbbox(trial)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
        if len(lines) >= max_lines:
            break

    if current and len(lines) < max_lines:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]

    if len(lines) == max_lines:
        joined = ' '.join(words)
        rendered = ' '.join(lines)
        if len(rendered) < len(joined):
            last = lines[-1]
            while last and font.getbbox(last + '…')[2] - font.getbbox(last + '…')[0] > max_width:
                last = last[:-1]
            lines[-1] = (last + '…') if last else '…'

    return lines

def _relative_luminance(r: int, g: int, b: int) -> float:
    """WCAG relative luminance — chọn màu chữ tương phản."""

    def channel(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

# Tính tỷ lệ tương phản giữa 2 màu.
def _contrast_ratio(l1: float, l2: float) -> float:
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)

# Lấy màu trung bình của vùng đặt chữ.
def _sample_text_area_stats(
    canvas: Image.Image,
    text_area: tuple[int, int, int, int],
) -> tuple[float, int, int, int]:
    """Trả (avg_luminance, avg_r, avg_g, avg_b) của vùng đặt chữ."""
    left, top, right, bottom = text_area
    region = canvas.crop((left, top, right, bottom)).convert('RGB')
    pixels = list(region.getdata())
    if not pixels:
        return 0.5, 128, 128, 128

    rs = [p[0] for p in pixels]
    gs = [p[1] for p in pixels]
    bs = [p[2] for p in pixels]
    avg_r = sum(rs) // len(rs)
    avg_g = sum(gs) // len(gs)
    avg_b = sum(bs) // len(bs)
    avg_lum = _relative_luminance(avg_r, avg_g, avg_b)
    return avg_lum, avg_r, avg_g, avg_b

# Chọn màu chữ tương phản.
def _pick_text_style(
    canvas: Image.Image,
    text_area: tuple[int, int, int, int],
    spec: dict[str, Any],
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int], int]:
    """
    Chọn fill + stroke + stroke_width sao cho đọc được trên nền THẬT tại vùng chữ.
    Không làm tối ảnh — chỉ đổi màu/viền chữ.
    """
    if spec.get('color_mode') == 'fixed':
        fill = tuple(spec.get('color', (255, 255, 255, 255)))
        stroke = (0, 0, 0, 220) if fill[0] > 128 else (255, 255, 255, 200)
        return fill, stroke, int(spec.get('stroke_width', 3))

    avg_lum, avg_r, avg_g, avg_b = _sample_text_area_stats(canvas, text_area)

    # Ứng viên: trắng, đen, vàng thumbnail (nền xanh/tím), trắng kem (nền tối ấm)
    candidates: list[tuple[tuple[int, int, int, int], str]] = [
        ((255, 255, 255, 255), 'white'),
        ((20, 20, 20, 255), 'black'),
        ((255, 230, 0, 255), 'yellow'),
        ((255, 248, 220, 255), 'cream'),
    ]

    bg_lum = avg_lum
    best_fill = candidates[0][0]
    best_ratio = 0.0

    for fill, _name in candidates:
        text_lum = _relative_luminance(fill[0], fill[1], fill[2])
        ratio = _contrast_ratio(bg_lum, text_lum)
        if ratio > best_ratio:
            best_ratio = ratio
            best_fill = fill

    # Nền quá sáng → ưu tiên đen; quá tối → ưu tiên trắng (override nếu contrast sát)
    if avg_lum > 0.62 and best_fill[0] > 200:
        best_fill = (20, 20, 20, 255)
    elif avg_lum < 0.38 and best_fill[0] < 80:
        best_fill = (255, 255, 255, 255)

    stroke = (0, 0, 0, 220) if best_fill[0] > 128 else (255, 255, 255, 200)
    stroke_width = 3 if best_ratio < 4.5 else 2  # nền lẫn → viền dày hơn

    logger.info(
        '[THUMBNAIL] text_area lum=%.2f contrast=%.1f fill=%s stroke_w=%s',
        avg_lum, best_ratio, 'light' if best_fill[0] > 128 else 'dark', stroke_width,
    )
    return best_fill, stroke, stroke_width

# Vẽ tiêu đề lên ảnh
def _draw_title(canvas: Image.Image, title: str, spec: dict[str, Any], font_key: str) -> None:
    left, top, right, bottom = spec['area']
    max_width = right - left
    max_height = bottom - top
    align = spec.get('align', 'center')
    max_lines = int(spec.get('max_lines', 3))
    font_size_start = int(spec.get('font_size_start', 72))
    font_size_min = int(spec.get('font_size_min', 28))

    fill, stroke_fill, stroke_width = _pick_text_style(canvas, spec['area'], spec)

    font_size = font_size_start
    lines: list[str] = []
    while font_size >= font_size_min:
        font = _load_font(font_key, font_size)
        lines = _wrap_title(title, font, max_width, max_lines)
        line_heights = [font.getbbox(line)[3] - font.getbbox(line)[1] for line in lines]
        total_h = sum(line_heights) + (len(lines) - 1) * int(font_size * 0.15)
        if total_h <= max_height:
            break
        font_size -= 2
    else:
        font = _load_font(font_key, font_size)

    draw = ImageDraw.Draw(canvas)
    line_gap = int(font_size * 0.15)
    line_heights = [font.getbbox(line)[3] - font.getbbox(line)[1] for line in lines]
    total_h = sum(line_heights) + (len(lines) - 1) * line_gap
    y = top + (max_height - total_h) // 2

    for i, line in enumerate(lines):
        bbox = font.getbbox(line)
        text_w = bbox[2] - bbox[0]
        x = left + (max_width - text_w) // 2 if align == 'center' else left
        draw.text(
            (x, y), line, font=font, fill=fill,
            stroke_width=stroke_width, stroke_fill=stroke_fill,
        )
        y += line_heights[i] + line_gap

# Tạo lớp nền — Gemini nếu bật, fallback PIL crop.
def _build_background(
    content_image_bytes: bytes,
    size: Tuple[int, int],
    spec: dict[str, Any],
    orientation: Orientation,
    enhance: bool,
) -> Image.Image:
    """Tạo lớp nền — Gemini nếu bật, fallback PIL crop."""
    content = _open_rgba(content_image_bytes).convert('RGB')

    if enhance:
        try:
            from video_management.services.thumbnail_gemini_service import (
                ThumbnailGeminiError,
                enhance_background_with_gemini,
            )
            logger.info('[THUMBNAIL] Gemini enhance background ON')
            return enhance_background_with_gemini(
                content_image_bytes,
                size,
                orientation,
            ).convert('RGBA')
        except ThumbnailGeminiError as exc:
            logger.warning('[THUMBNAIL] Gemini fallback PIL: %s', exc)
        except Exception:
            logger.warning('[THUMBNAIL] Gemini fallback PIL', exc_info=True)

    bg = _fit_cover(content, size).convert('RGBA')
    blur = int(spec.get('content_bg', {}).get('blur', 0))
    if blur > 0:
        bg = bg.filter(ImageFilter.GaussianBlur(blur))
    return bg

# Post polish: sharpen + saturation nhẹ — free, không tốn API.
def _post_polish(canvas: Image.Image) -> Image.Image:
    """Sharpen + saturation nhẹ — free, không tốn API."""
    from PIL import ImageEnhance

    rgb = canvas.convert('RGB')
    rgb = ImageEnhance.Color(rgb).enhance(1.08)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.05)
    rgb = rgb.filter(ImageFilter.UnsharpMask(radius=1.2, percent=80, threshold=3))
    return rgb.convert('RGBA')

# Tạo thumbnail từ dữ liệu đầu vào
def generate_thumbnail(data: ThumbnailInput) -> bytes:
    if data.template not in TEMPLATES:
        raise ValueError(f'Template không hỗ trợ: {data.template}')
    if data.orientation not in TEMPLATES[data.template]:
        raise ValueError(f'Orientation không hỗ trợ: {data.orientation}')
    if len(data.content_image_bytes) > MAX_CONTENT_BYTES:
        raise ValueError(f'Ảnh nội dung tối đa {MAX_CONTENT_BYTES // (1024 * 1024)}MB')

    spec = TEMPLATES[data.template][data.orientation]
    size = tuple(spec['size'])

    bg = _build_background(
        data.content_image_bytes,
        size,
        spec,
        data.orientation,
        data.enhance_background,
    )
    canvas = bg.copy()

    person = _open_rgba(data.person_image_bytes)
    _paste_person(canvas, person, spec['person'])
    _draw_title(canvas, data.title, spec['text'], data.font_key)

    canvas = _post_polish(canvas)
    out = io.BytesIO()
    canvas.convert('RGB').save(out, format='PNG', optimize=True)
    return out.getvalue()

# Load ảnh người từ assets
def load_person_bytes_by_slug(slug: str) -> bytes:
    """Đọc PNG người từ assets — slug phải khớp tên file (vd. ST1 → ST1.png)."""
    slug = (slug or '').strip()
    if not PERSON_SLUG_RE.fullmatch(slug):
        raise ValueError('Slug người không hợp lệ')

    path = os.path.realpath(os.path.join(PERSONS_DIR, f'{slug}.png'))
    persons_root = os.path.realpath(PERSONS_DIR)
    if not path.startswith(persons_root + os.sep):
        raise ValueError('Slug người không hợp lệ')
    if not os.path.isfile(path):
        raise FileNotFoundError(f'Không tìm thấy person asset: {slug}')

    with open(path, 'rb') as handle:
        return handle.read()

# Tải ảnh từ URL
def download_image_bytes(url: str, timeout: int = DOWNLOAD_TIMEOUT_S) -> bytes:
    url = (url or '').strip()
    if not is_public_http_url(url):
        raise ValueError('URL ảnh người không hợp lệ hoặc không được phép')

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    content_type = (resp.headers.get('Content-Type') or '').lower()
    if content_type and not content_type.startswith('image/'):
        raise ValueError('URL không trả về ảnh hợp lệ')
    if len(resp.content) > MAX_CONTENT_BYTES:
        raise ValueError(f'Ảnh tải về vượt quá {MAX_CONTENT_BYTES // (1024 * 1024)}MB')
    return resp.content
