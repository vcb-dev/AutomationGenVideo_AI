"""Unit tests for deterministic thumbnail template engine."""
from __future__ import annotations

import io
import os
from unittest.mock import patch

from django.test import SimpleTestCase
from PIL import Image

from video_management.services.thumbnail_service import (
    ThumbnailInput,
    download_image_bytes,
    generate_thumbnail,
    list_templates,
    load_person_bytes_by_slug,
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
    # Kiểm tra danh sách template chỉ trả về các font thực sự tồn tại trên hệ thống
    def test_list_templates_only_includes_existing_fonts(self):
        data = list_templates()
        self.assertIn('simple_v1', [t['id'] for t in data['templates']])
        self.assertTrue(all(t['id'] == 'bevietnam_bold' for t in data['fonts']))

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
