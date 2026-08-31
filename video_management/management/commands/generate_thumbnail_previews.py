"""Sinh ảnh preview cho từng template × orientation.

Sửa TEMPLATES trong thumbnail_service.py và chạy:
cd /Users/macos/Documents/VCBI/AutomationGenVideo_AI
python manage.py generate_thumbnail_previews

Copy preview sang FE (nếu dùng static):
cp video_management/assets/thumbnail/previews/*.png \
   ../AutomationGenVideo_FE/public/thumbnail/previews/
"""
from __future__ import annotations

import io
import os

from django.core.management.base import BaseCommand
from PIL import Image, ImageDraw

from video_management.services.thumbnail_service import (
    PERSONS_DIR,
    PREVIEWS_DIR,
    TEMPLATE_CATALOG,
    TEMPLATES,
    ThumbnailInput,
    generate_thumbnail,
)


def _sample_content_png(size: tuple[int, int]) -> bytes:
    """Gradient nền mẫu — không cần upload."""
    w, h = size
    img = Image.new('RGB', size)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(30 + 80 * t)
        g = int(90 + 60 * (1 - t))
        b = int(180 - 50 * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def _sample_person_png() -> bytes:
    preview_path = os.path.join(PERSONS_DIR, 'preview_person.png')
    if os.path.isfile(preview_path):
        with open(preview_path, 'rb') as handle:
            return handle.read()

    # Fallback: silhouette đơn giản
    img = Image.new('RGBA', (200, 320), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([60, 20, 140, 100], fill=(220, 180, 160, 230))
    draw.rectangle([50, 110, 150, 300], fill=(60, 100, 180, 220))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


class Command(BaseCommand):
    help = 'Generate thumbnail template preview PNGs into assets/thumbnail/previews/'

    def handle(self, *args, **options):
        os.makedirs(PREVIEWS_DIR, exist_ok=True)
        content_cache: dict[tuple[int, int], bytes] = {}
        person_bytes = _sample_person_png()
        sample_title = 'TIÊU ĐỀ\nTHUMBNAIL MẪU'

        count = 0
        for template_id, cfg in TEMPLATES.items():
            label = TEMPLATE_CATALOG.get(template_id, {}).get('label', template_id)
            for orientation, spec in cfg.items():
                size = tuple(spec['size'])
                if size not in content_cache:
                    content_cache[size] = _sample_content_png(size)

                png = generate_thumbnail(
                    ThumbnailInput(
                        title=sample_title,
                        template=template_id,
                        orientation=orientation,  # type: ignore[arg-type]
                        font_key='bevietnam_bold',
                        content_image_bytes=content_cache[size],
                        person_image_bytes=person_bytes,
                        enhance_background=False,
                    )
                )
                out_name = f'{template_id}_{orientation}.png'
                out_path = os.path.join(PREVIEWS_DIR, out_name)
                with open(out_path, 'wb') as handle:
                    handle.write(png)
                count += 1
                self.stdout.write(self.style.SUCCESS(f'  ✓ {label} / {orientation} → {out_name}'))

        self.stdout.write(self.style.SUCCESS(f'Done — {count} previews in {PREVIEWS_DIR}'))
