"""Unit tests for deterministic thumbnail template engine."""
from __future__ import annotations

import io
import os
from unittest.mock import patch

from django.test import SimpleTestCase
from PIL import Image, ImageFont

from video_management.services.thumbnail_service import (
    FONTS_DIR,
    TEMPLATES,
    ThumbnailInput,
    _layout_text_lines,
    _paste_person,
    build_text_spec,
    download_image_bytes,
    generate_thumbnail,
    list_templates,
    load_person_bytes_by_slug,
    parse_hex_color,
)


def _solid_png(color: tuple[int, int, int], size: tuple[int, int] = (400, 400)) -> bytes:
    img = Image.new('RGB', size, color)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def _transparent_person_png(size: tuple[int, int] = (200, 300)) -> bytes:
    img = Image.new('RGBA', size, (255, 0, 0, 200))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


class ThumbnailServiceTests(SimpleTestCase):
    def test_multilingual_line_layout_does_not_overlap(self):
        font = ImageFont.truetype(os.path.join(FONTS_DIR, 'BeVietnamPro-Bold.ttf'), 76)
        lines = ['TIÊU ĐỀ', 'THUMBNAIL', 'MẪU']
        gap = 11
        layouts, _total_height = _layout_text_lines(font, lines, gap, stroke_width=2)

        visual_ranges = [
            (baseline + bbox[1], baseline + bbox[3])
            for bbox, baseline in layouts
        ]
        for previous, current in zip(visual_ranges, visual_ranges[1:]):
            self.assertGreaterEqual(current[0] - previous[1], gap)

    # Kiểm tra parse hex color hợp lệ / không hợp lệ
    def test_parse_hex_color(self):
        self.assertEqual(parse_hex_color('#FF0000'), (255, 0, 0, 255))
        with self.assertRaises(ValueError):
            parse_hex_color('red')

    # Kiểm tra build_text_spec merge override
    def test_build_text_spec_overrides(self):
        base = {'color_mode': 'auto', 'font_size_start': 72}
        spec = build_text_spec(base, 96, '#00FF00')
        self.assertEqual(spec['font_size_start'], 96)
        self.assertEqual(spec['color_mode'], 'fixed')
        self.assertEqual(spec['color'], (0, 255, 0, 255))

    # Kiểm tra generate với cỡ chữ và màu cố định
    def test_generate_with_custom_font_size_and_color(self):
        png = generate_thumbnail(
            ThumbnailInput(
                title='Custom style',
                template='simple_v1',
                orientation='landscape',
                font_key='bevietnam_bold',
                content_image_bytes=_solid_png((30, 120, 200)),
                person_image_bytes=_transparent_person_png(),
                font_size=96,
                text_color='#FF0000',
            )
        )
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.size, (1280, 720))

    # Kiểm tra danh sách template có metadata preview + nhiều font
    def test_list_templates_metadata(self):
        data = list_templates()
        self.assertGreaterEqual(len(data['templates']), 6)
        self.assertGreaterEqual(len(data['fonts']), 6)
        tpl = next(t for t in data['templates'] if t['id'] == 'simple_v1')
        self.assertIn('label', tpl)
        self.assertIn('platforms', tpl)
        self.assertIn('previews', tpl)
        font = next(f for f in data['fonts'] if f['id'] == 'bevietnam_bold')
        self.assertIn('sample', font)
        self.assertIn('regions', font)

    def test_bold_side_templates_support_both_orientations(self):
        for orientation in ('landscape', 'portrait'):
            self.assertEqual(TEMPLATES['bold_left_v1'][orientation]['person']['anchor'], 'bottom_right')
            self.assertEqual(TEMPLATES['bold_right_v1'][orientation]['person']['anchor'], 'bottom_left')

    def test_portrait_templates_place_person_flush_with_bottom(self):
        for orientations in TEMPLATES.values():
            self.assertEqual(orientations['portrait']['person']['bottom_padding'], 0)

    def test_side_anchor_ignores_transparent_person_margin(self):
        person = Image.new('RGBA', (100, 100), (0, 0, 0, 0))
        person.paste((255, 0, 0, 255), (30, 20, 80, 80))
        person.putpixel((0, 0), (255, 0, 0, 1))

        for anchor, expected_left, expected_right in (
            ('bottom_left', 0, None),
            ('bottom_right', None, 200),
        ):
            canvas = Image.new('RGBA', (200, 100), (0, 0, 0, 0))
            _paste_person(canvas, person, {'anchor': anchor, 'max_height_ratio': 1})
            visible_alpha = canvas.getchannel('A').point(lambda value: 255 if value >= 16 else 0)
            bounds = visible_alpha.getbbox()
            self.assertIsNotNone(bounds)
            if expected_left is not None:
                self.assertEqual(bounds[0], expected_left)
            if expected_right is not None:
                self.assertEqual(bounds[2], expected_right)
            self.assertEqual(bounds[3], 100)

    # Kiểm tra tạo thumbnail ngang (1280x720) với template simple_v1
    def test_generate_landscape_png(self):
        png = generate_thumbnail(
            ThumbnailInput(
                title='Tiêu đề thử nghiệm thumbnail',
                template='simple_v1',
                orientation='landscape',
                font_key='bevietnam_bold',
                content_image_bytes=_solid_png((30, 120, 200)),
                person_image_bytes=_transparent_person_png(),
            )
        )
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.size, (1280, 720))
        self.assertEqual(img.format, 'PNG')

    # Kiểm tra tạo thumbnail dọc (720x1280) với template simple_v1
    def test_generate_portrait_png(self):
        png = generate_thumbnail(
            ThumbnailInput(
                title='Dọc',
                template='simple_v1',
                orientation='portrait',
                font_key='bevietnam_bold',
                content_image_bytes=_solid_png((10, 10, 10)),
                person_image_bytes=_transparent_person_png(),
            )
        )
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.size, (720, 1280))

    # Kiểm tra truyền template không tồn tại phải raise ValueError
    def test_invalid_template_raises(self):
        with self.assertRaises(ValueError):
            generate_thumbnail(
                ThumbnailInput(
                    title='x',
                    template='missing',
                    orientation='landscape',
                    font_key='bevietnam_bold',
                    content_image_bytes=_solid_png((0, 0, 0)),
                    person_image_bytes=_transparent_person_png(),
                )
            )

    # Kiểm tra slug ảnh người có path traversal (../) phải bị từ chối
    def test_load_person_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            load_person_bytes_by_slug('../etc/passwd')

    # Kiểm tra tải ảnh từ URL nội bộ/private (127.0.0.1) phải bị chặn SSRF
    def test_download_image_blocks_private_url(self):
        with self.assertRaises(ValueError):
            download_image_bytes('http://127.0.0.1/secret.png')

    # Kiểm tra tải URL hợp lệ nhưng Content-Type không phải ảnh phải raise ValueError
    @patch('video_management.services.thumbnail_service.requests.get')
    @patch('video_management.services.thumbnail_service.is_public_http_url', return_value=True)
    def test_download_image_validates_content_type(self, _mock_public, mock_get):
        mock_get.return_value.headers = {'Content-Type': 'text/html'}
        mock_get.return_value.content = b'<html></html>'
        with self.assertRaises(ValueError):
            download_image_bytes('https://example.com/not-image')

    # Kiểm tra load asset ST1.png nếu file tồn tại trong thư mục persons
    def test_load_existing_asset_if_present(self):
        persons_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'video_management',
            'assets',
            'thumbnail',
            'persons',
        )
        st1 = os.path.join(persons_dir, 'ST1.png')
        if os.path.isfile(st1):
            data = load_person_bytes_by_slug('ST1')
            self.assertTrue(len(data) > 100)

    # Kiểm tra enhance_background=True nhưng Gemini lỗi vẫn tạo được thumbnail (fallback)
    @patch('video_management.services.thumbnail_gemini_service.enhance_background_with_gemini')
    def test_generate_with_enhance_fallback_when_gemini_fails(self, mock_gemini):
        from video_management.services.thumbnail_gemini_service import ThumbnailGeminiError

        mock_gemini.side_effect = ThumbnailGeminiError('quota')

        png = generate_thumbnail(
            ThumbnailInput(
                title='Fallback test',
                template='simple_v1',
                orientation='landscape',
                font_key='bevietnam_bold',
                content_image_bytes=_solid_png((30, 120, 200)),
                person_image_bytes=_transparent_person_png(),
                enhance_background=True,
            )
        )
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.size, (1280, 720))
