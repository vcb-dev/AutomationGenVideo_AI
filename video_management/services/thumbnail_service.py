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
                'color': (255, 255, 255, 255),
                'align': 'center',
                'max_lines': 3,
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
                'color': (255, 255, 255, 255),
                'align': 'center',
                'max_lines': 4,
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

# Vẽ tiêu đề lên ảnh
def _draw_title(canvas: Image.Image, title: str, spec: dict[str, Any], font_key: str) -> None:
    left, top, right, bottom = spec['area']
    max_width = right - left
    max_height = bottom - top
    align = spec.get('align', 'center')
    color = tuple(spec.get('color', (255, 255, 255, 255)))
    max_lines = int(spec.get('max_lines', 3))

    font_size = 72
    lines: list[str] = []
    font = _load_font(font_key, font_size)

    while font_size >= 28:
        font = _load_font(font_key, font_size)
        lines = _wrap_title(title, font, max_width, max_lines)
        line_heights = [font.getbbox(line)[3] - font.getbbox(line)[1] for line in lines]
        total_h = sum(line_heights) + (len(lines) - 1) * int(font_size * 0.15)
        if total_h <= max_height:
            break
        font_size -= 2

    draw = ImageDraw.Draw(canvas)
    line_gap = int(font_size * 0.15)
    line_heights = [font.getbbox(line)[3] - font.getbbox(line)[1] for line in lines]
    total_h = sum(line_heights) + (len(lines) - 1) * line_gap
    y = top + (max_height - total_h) // 2

    for i, line in enumerate(lines):
        bbox = font.getbbox(line)
        text_w = bbox[2] - bbox[0]
        x = left + (max_width - text_w) // 2 if align == 'center' else left
        draw.text((x, y), line, font=font, fill=color)
        y += line_heights[i] + line_gap

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

    content = _open_rgba(data.content_image_bytes).convert('RGB')
    bg = _fit_cover(content, size).convert('RGBA')

    blur = int(spec.get('content_bg', {}).get('blur', 0))
    if blur > 0:
        bg = bg.filter(ImageFilter.GaussianBlur(blur))

    overlay_color = tuple(spec.get('overlay', {}).get('color', (0, 0, 0, 80)))
    overlay = Image.new('RGBA', size, overlay_color)
    canvas = Image.alpha_composite(bg, overlay)

    person = _open_rgba(data.person_image_bytes)
    _paste_person(canvas, person, spec['person'])
    _draw_title(canvas, data.title, spec['text'], data.font_key)

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
