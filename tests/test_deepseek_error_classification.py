"""DeepSeekError phải phân loại đúng — thay cho `_call_deepseek_raw` cũ nuốt mọi lỗi
thành None (2c8c744).

Trước đây timeout / 429 rate-limit / 5xx của DeepSeek / JSON hỏng đều quy về cùng một
triệu chứng "DeepSeek không phản hồi" → BE báo "có thể do timeout" cho MỌI trường hợp,
không thể biết một lượt chấm PAAST hỏng vì chậm thật hay vì bị chặn rate-limit. Test này
khoá lại: mỗi loại lỗi HTTP/mạng phải ánh xạ đúng `kind`, và `retriable` phải phân biệt
đúng lỗi ngẫu nhiên (đáng thử lại) với lỗi tất định (thử lại vô ích).

Chạy: python manage.py test tests.test_deepseek_error_classification
"""

from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase

from video_management.services.content_generation_service import (
    ContentGenerationService,
    DeepSeekError,
)


def _response(status_code, json_body=None, text='', headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.headers = headers or {}
    r.text = text
    if json_body is not None:
        r.json.return_value = json_body
    else:
        r.json.side_effect = ValueError('not json')
    return r


def _ok_json(content='xin chào', finish_reason='stop', completion_tokens=10):
    return {
        'choices': [{'message': {'content': content}, 'finish_reason': finish_reason}],
        'usage': {'completion_tokens': completion_tokens},
    }


def _build_service():
    service = ContentGenerationService()
    service.deepseek_key = 'test-key'  # tránh phụ thuộc DEEPSEEK_API_KEY thật của môi trường
    return service


class DeepSeekErrorClassificationTests(SimpleTestCase):
    def setUp(self):
        self.service = _build_service()

    def test_khong_co_api_key_nem_kind_no_key(self):
        self.service.deepseek_key = ''

        with self.assertRaises(DeepSeekError) as ctx:
            self.service._call_deepseek_checked(prompt='p', system_msg='s')

        self.assertEqual(ctx.exception.kind, 'no_key')
        self.assertFalse(ctx.exception.retriable)

    def test_requests_timeout_nem_kind_timeout_va_retriable(self):
        with patch('video_management.services.content_generation_service.requests.post') as mock_post:
            mock_post.side_effect = requests.exceptions.Timeout('timed out')

            with self.assertRaises(DeepSeekError) as ctx:
                self.service._call_deepseek_checked(prompt='p', system_msg='s', timeout=5)

        self.assertEqual(ctx.exception.kind, 'timeout')
        self.assertTrue(ctx.exception.retriable)

    def test_loi_mang_nem_kind_network_va_retriable(self):
        with patch('video_management.services.content_generation_service.requests.post') as mock_post:
            mock_post.side_effect = requests.exceptions.ConnectionError('dns fail')

            with self.assertRaises(DeepSeekError) as ctx:
                self.service._call_deepseek_checked(prompt='p', system_msg='s')

        self.assertEqual(ctx.exception.kind, 'network')
        self.assertTrue(ctx.exception.retriable)

    def test_429_het_luot_backoff_nem_kind_rate_limit_va_retriable(self):
        with patch('video_management.services.content_generation_service.requests.post') as mock_post, \
                patch('video_management.services.content_generation_service.time.sleep'):
            mock_post.return_value = _response(429, headers={'Retry-After': '0'})

            with self.assertRaises(DeepSeekError) as ctx:
                self.service._call_deepseek_checked(
                    prompt='p', system_msg='s', rate_limit_retries=2,
                )

        self.assertEqual(ctx.exception.kind, 'rate_limit')
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertTrue(ctx.exception.retriable)
        # 1 lượt đầu + rate_limit_retries lượt backoff = 3 lần gọi.
        self.assertEqual(mock_post.call_count, 3)

    def test_429_roi_thanh_cong_o_lan_backoff_thu_hai(self):
        """429 chỉ hỏng tạm thời — retry tại chỗ phải cứu được, không ném lỗi lên tầng trên."""
        with patch('video_management.services.content_generation_service.requests.post') as mock_post, \
                patch('video_management.services.content_generation_service.time.sleep') as mock_sleep:
            mock_post.side_effect = [
                _response(429, headers={'Retry-After': '1'}),
                _response(200, json_body=_ok_json('kết quả sau retry')),
            ]

            content = self.service._call_deepseek_checked(prompt='p', system_msg='s')

        self.assertEqual(content, 'kết quả sau retry')
        mock_sleep.assert_called_once_with(1.0)

    def test_5xx_nem_kind_server_va_retriable(self):
        with patch('video_management.services.content_generation_service.requests.post') as mock_post:
            mock_post.return_value = _response(503, text='service unavailable')

            with self.assertRaises(DeepSeekError) as ctx:
                self.service._call_deepseek_checked(prompt='p', system_msg='s')

        self.assertEqual(ctx.exception.kind, 'server')
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertTrue(ctx.exception.retriable)

    def test_4xx_khac_nem_kind_client_va_KHONG_retriable(self):
        """4xx không phải 429 là lỗi tất định (request sai) — thử lại không đổi kết quả."""
        with patch('video_management.services.content_generation_service.requests.post') as mock_post:
            mock_post.return_value = _response(400, text='bad request')

            with self.assertRaises(DeepSeekError) as ctx:
                self.service._call_deepseek_checked(prompt='p', system_msg='s')

        self.assertEqual(ctx.exception.kind, 'client')
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(ctx.exception.retriable)

    def test_response_sai_shape_nem_kind_parse_va_KHONG_retriable(self):
        with patch('video_management.services.content_generation_service.requests.post') as mock_post:
            mock_post.return_value = _response(200, json_body={'unexpected': 'shape'})

            with self.assertRaises(DeepSeekError) as ctx:
                self.service._call_deepseek_checked(prompt='p', system_msg='s')

        self.assertEqual(ctx.exception.kind, 'parse')
        self.assertFalse(ctx.exception.retriable)

    def test_content_rong_vi_het_token_reasoning_nem_kind_token_budget(self):
        """finish_reason='length': token suy luận nội bộ ăn hết max_tokens — lỗi NGÂN SÁCH
        token, không phải timeout, và đáng thử lại (tăng max_tokens/tắt reasoning ở lượt sau).

        Mang kind riêng 'token_budget' thay vì 'server': vẫn retriable như trước, nhưng câu báo
        cho người dùng thôi đổ lỗi cho nhà cung cấp trong khi lỗi là ở ngân sách của chính mình."""
        with patch('video_management.services.content_generation_service.requests.post') as mock_post:
            mock_post.return_value = _response(200, json_body=_ok_json(content='', finish_reason='length'))

            with self.assertRaises(DeepSeekError) as ctx:
                self.service._call_deepseek_checked(prompt='p', system_msg='s')

        self.assertEqual(ctx.exception.kind, 'token_budget')
        self.assertTrue(ctx.exception.retriable)

    def test_content_rong_vi_ly_do_khac_nem_kind_parse(self):
        with patch('video_management.services.content_generation_service.requests.post') as mock_post:
            mock_post.return_value = _response(200, json_body=_ok_json(content='', finish_reason='stop'))

            with self.assertRaises(DeepSeekError) as ctx:
                self.service._call_deepseek_checked(prompt='p', system_msg='s')

        self.assertEqual(ctx.exception.kind, 'parse')
        self.assertFalse(ctx.exception.retriable)

    def test_thanh_cong_tra_dung_content(self):
        with patch('video_management.services.content_generation_service.requests.post') as mock_post:
            mock_post.return_value = _response(200, json_body=_ok_json('nội dung hợp lệ'))

            content = self.service._call_deepseek_checked(prompt='p', system_msg='s')

        self.assertEqual(content, 'nội dung hợp lệ')

    def test_call_deepseek_raw_nuot_DeepSeekError_tra_None_giu_contract_cu(self):
        """`_call_deepseek_raw` (14 nơi gọi cũ) phải GIỮ hành vi None-khi-lỗi — chỉ
        `_call_deepseek_checked` mới ném lỗi có phân loại cho nơi cần biết (PAAST)."""
        with patch('video_management.services.content_generation_service.requests.post') as mock_post:
            mock_post.return_value = _response(500, text='boom')

            result = self.service._call_deepseek_raw(prompt='p', system_msg='s')

        self.assertIsNone(result)
