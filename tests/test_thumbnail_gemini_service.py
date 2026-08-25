"""Tests for Gemini thumbnail background — mock API, không gọi thật."""
from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from PIL import Image

from video_management.services.thumbnail_gemini_service import (
    ThumbnailGeminiError,
    _extract_image_bytes,
    enhance_background_with_gemini,
)

from video_management.services.thumbnail_gemini_service import (
    generate_content_with_gemini,
)


def _fake_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (800, 600), (100, 150, 200)).save(buf, format='PNG')
    return buf.getvalue()


def _fake_gemini_response(png_bytes: bytes):
    part = MagicMock()
    part.inline_data.mime_type = 'image/png'
    part.inline_data.data = png_bytes

    content = MagicMock()
    content.parts = [part]

    candidate = MagicMock()
    candidate.content = content

    response = MagicMock()
    response.candidates = [candidate]
    return response


class ThumbnailGeminiServiceTests(SimpleTestCase):
    # Kiểm tra trích xuất bytes ảnh PNG từ response Gemini hợp lệ
    def test_extract_image_bytes_ok(self):
        png = _fake_png_bytes()
        data = _extract_image_bytes(_fake_gemini_response(png))
        self.assertEqual(data, png)

    # Kiểm tra response Gemini không có candidate phải raise ThumbnailGeminiError
    def test_extract_image_bytes_empty_raises(self):
        response = MagicMock()
        response.candidates = []
        with self.assertRaises(ThumbnailGeminiError):
            _extract_image_bytes(response)

    # Kiểm tra enhance_background trả về ảnh đúng kích thước target (1280x720 landscape)
    @patch('google.generativeai.GenerativeModel')
    @patch('google.generativeai.configure')
    def test_enhance_returns_correct_size(self, _mock_configure, mock_generative_model):
        out_png = io.BytesIO()
        Image.new('RGB', (1600, 900), (255, 0, 0)).save(out_png, format='PNG')

        mock_model = MagicMock()
        mock_model.generate_content.return_value = _fake_gemini_response(out_png.getvalue())
        mock_generative_model.return_value = mock_model

        with patch(
            'video_management.services.thumbnail_gemini_service._gemini_api_key',
            return_value='test-key',
        ):
            result = enhance_background_with_gemini(_fake_png_bytes(), (1280, 720), 'landscape')

        self.assertEqual(result.size, (1280, 720))
    # Kiểm tra generate_content_with_gemini trả về ảnh đúng kích thước target (1280x720 landscape)
    @patch('google.generativeai.GenerativeModel')
    @patch('google.generativeai.configure')
    def test_generate_content_returns_correct_size(self, _mock_configure, mock_generative_model):
        out_png = io.BytesIO()
        Image.new('RGB', (1600, 900), (0, 128, 255)).save(out_png, format='PNG')
        mock_model = MagicMock()
        mock_model.generate_content.return_value = _fake_gemini_response(out_png.getvalue())
        mock_generative_model.return_value = mock_model
        with patch(
            'video_management.services.thumbnail_gemini_service._gemini_api_key',
            return_value='test-key',
        ):
            result = generate_content_with_gemini(
                prompt='Phòng gym hiện đại, ánh sáng vàng',
                orientation='landscape',
                reference_images=[_fake_png_bytes()],
            )
        self.assertEqual(result.size, (1280, 720))
        # Kiểm tra gọi Gemini có prompt + 1 ref image
        call_parts = mock_model.generate_content.call_args[0][0]
        self.assertGreaterEqual(len(call_parts), 2)