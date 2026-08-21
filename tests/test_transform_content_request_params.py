"""`transform_content` phải đọc timeout_seconds/max_tokens/temperature từ body request thay vì
rơi về default hard-code (688fbd4).

Trước đây view này không đọc timeout từ body, nên mọi lệnh gọi `_call_deepseek_raw()` rơi về
default hard-code 60s bất kể BE đặt timeout axios 120s (timeout đó chỉ áp dụng cho chính
request HTTP BE<->AI service, AI service không có cách nào biết để nới ra). Input cần >60s
suy luận bị cắt ngang giữa chừng, trả 502 chung chung khiến rất khó chẩn đoán là do content
dài chứ không phải DeepSeek thật sự lỗi.

Chạy: python manage.py test tests.test_transform_content_request_params
"""

from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from video_management.views import content_generation_views


class TransformContentRequestParamsTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _post(self, **body):
        payload = {'system_prompt': 'Bạn là biên tập viên', 'input_text': 'nội dung gốc'}
        payload.update(body)
        return self.factory.post('/api/ai/transform-content/', payload, format='json')

    def _call(self, **body):
        request = self._post(**body)
        with patch.object(content_generation_views, 'ContentGenerationService') as ServiceCls:
            ServiceCls.return_value._call_deepseek_checked.return_value = 'output đã viết lại'
            response = content_generation_views.transform_content(request)
            call_kwargs = ServiceCls.return_value._call_deepseek_checked.call_args.kwargs
        return response, call_kwargs

    def test_khong_gui_timeout_thi_dung_default_120s_khong_phai_60s(self):
        """Trước fix: rơi về default hard-code 60s của _call_deepseek_raw. Sau fix: view tự
        đặt fallback 120s, khớp timeout axios 120s mà BE thật sự đang chờ."""
        _, call_kwargs = self._call()

        self.assertEqual(call_kwargs['timeout'], 120)

    def test_gui_timeout_seconds_thi_dung_dung_gia_tri_do(self):
        _, call_kwargs = self._call(timeout_seconds=240)

        self.assertEqual(call_kwargs['timeout'], 240)
        self.assertIsInstance(call_kwargs['timeout'], int)

    def test_gui_max_tokens_thi_duoc_truyen_xuong(self):
        _, call_kwargs = self._call(max_tokens=16000)

        self.assertEqual(call_kwargs['max_tokens'], 16000)

    def test_khong_gui_max_tokens_thi_khong_ep_gia_tri_nao(self):
        """Không gửi thì phải để _call_deepseek_raw tự dùng default riêng của nó (2048),
        không phải view tự áp một con số nào khác."""
        _, call_kwargs = self._call()

        self.assertNotIn('max_tokens', call_kwargs)

    def test_gui_temperature_thi_duoc_truyen_xuong_dang_float(self):
        _, call_kwargs = self._call(temperature='0.7')

        self.assertEqual(call_kwargs['temperature'], 0.7)
        self.assertIsInstance(call_kwargs['temperature'], float)

    def test_khong_gui_temperature_thi_khong_ep_gia_tri_nao(self):
        _, call_kwargs = self._call()

        self.assertNotIn('temperature', call_kwargs)

    def test_ca_ba_tham_so_cung_luc(self):
        _, call_kwargs = self._call(timeout_seconds=90, max_tokens=4096, temperature=0.3)

        self.assertEqual(call_kwargs['timeout'], 90)
        self.assertEqual(call_kwargs['max_tokens'], 4096)
        self.assertEqual(call_kwargs['temperature'], 0.3)

    def test_thieu_system_prompt_van_400_truoc_khi_doc_cac_tham_so(self):
        request = self.factory.post(
            '/api/ai/transform-content/', {'input_text': 'nội dung'}, format='json',
        )

        response = content_generation_views.transform_content(request)

        self.assertEqual(response.status_code, 400)
