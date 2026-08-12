"""Chức năng: view transform_content nói ĐÚNG lý do DeepSeek hỏng, thay vì đoán bừa.

Vì sao đáng một file test riêng: chuỗi lỗi ở đây không nằm lại trong log. BE lưu nó vào
content_transform.error_message rồi FE hiện nguyên văn trong toast, nên nó là câu người dùng
cuối đọc được. Bản cũ trả đúng một câu "Please check API key configuration or logs" cho mọi
tình huống — đo trên production 12/08/2026, một lượt hỏng vì DeepSeek từ chối vẫn báo là lỗi
cấu hình khoá, đủ để người vận hành đi kiểm tra nhầm chỗ.
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from unittest.mock import patch  # noqa: E402
from django.test import SimpleTestCase  # noqa: E402
from rest_framework import status  # noqa: E402
from rest_framework.test import APIRequestFactory  # noqa: E402

from video_management.services.content_generation_service import DeepSeekError  # noqa: E402
from video_management.views.content_generation_views import (  # noqa: E402
    DEEPSEEK_ERROR_MESSAGES,
    transform_content,
)


def _goi(loi=None, tra_ve=None):
    """Gọi view với DeepSeek đã bị thay bằng lỗi hoặc kết quả cho sẵn."""
    factory = APIRequestFactory()
    request = factory.post(
        '/api/ai/transform-content/',
        {'system_prompt': 'Viết lại cho hay hơn', 'input_text': 'Nhẫn vàng 18k giảm 20%.'},
        format='json',
    )
    target = 'video_management.views.content_generation_views.ContentGenerationService'
    with patch(target) as Service:
        instance = Service.return_value
        if loi is not None:
            instance._call_deepseek_checked.side_effect = loi
        else:
            instance._call_deepseek_checked.return_value = tra_ve
        return transform_content(request)


class TransformContentBaoDungLyDo(SimpleTestCase):
    def test_thieu_khoa_bao_dung_la_loi_cau_hinh(self):
        res = _goi(loi=DeepSeekError('no_key', 'Chưa cấu hình DEEPSEEK_API_KEY'))

        self.assertEqual(res.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(res.data['reason'], 'no_key')
        self.assertIn('DEEPSEEK_API_KEY', res.data['error'])
        # Thiếu khoá thì thử lại bao nhiêu lần cũng vậy — phải nói rõ để khỏi ai ngồi bấm lại.
        self.assertFalse(res.data['retriable'])

    def test_bi_tu_choi_khong_con_bao_la_loi_cau_hinh_khoa(self):
        res = _goi(loi=DeepSeekError('client', 'HTTP 402', status_code=402))

        self.assertEqual(res.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(res.data['reason'], 'client')
        self.assertIn('hết số dư', res.data['error'])
        # Đây chính là chỗ bản cũ nói sai: đổ cho cấu hình khoá trong khi khoá vẫn đúng.
        self.assertNotIn('chưa cấu hình', res.data['error'].lower())

    def test_cham_rate_limit_tra_429_va_bao_cho_cho(self):
        res = _goi(loi=DeepSeekError('rate_limit', 'HTTP 429', status_code=429))

        self.assertEqual(res.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(res.data['reason'], 'rate_limit')
        self.assertTrue(res.data['retriable'])

    def test_timeout_va_loi_may_chu_deu_502_va_deu_dang_thu_lai(self):
        for kind in ('timeout', 'server', 'network'):
            with self.subTest(kind=kind):
                res = _goi(loi=DeepSeekError(kind, 'hỏng'))
                self.assertEqual(res.status_code, status.HTTP_502_BAD_GATEWAY)
                self.assertEqual(res.data['reason'], kind)
                self.assertTrue(res.data['retriable'])

    def test_tra_ve_rong_van_bao_loi_chu_khong_bao_thanh_cong(self):
        res = _goi(tra_ve='')

        self.assertEqual(res.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(res.data['reason'], 'empty')

    def test_chay_duoc_thi_tra_output(self):
        res = _goi(tra_ve='Nhẫn vàng 18k đang giảm 20%, ghé xem ngay.')

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['success'])
        self.assertIn('Nhẫn vàng', res.data['output_text'])

    def test_moi_cau_bao_deu_bang_tieng_viet_va_noi_duoc_viec_can_lam(self):
        """Chuỗi này hiện thẳng cho người dùng cuối, không phải log kỹ thuật."""
        for kind, message in DEEPSEEK_ERROR_MESSAGES.items():
            with self.subTest(kind=kind):
                self.assertNotIn('Please check', message)
                self.assertGreater(len(message), 30)
