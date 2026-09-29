"""Tạo ảnh sản phẩm — chi phí từng lượt Gemini (chế độ chị Nhạm cầm SP).

Khoá: đọc đơn giá theo TÊN model từ GEMINI_IMAGE_PRICES_JSON (thiếu/sai thì lượt vẫn chạy nhưng
báo rõ tên biến, không tự dùng giá khác), tách token ảnh / chữ / suy nghĩ để tính đúng 3 mức giá
của Google, và view luôn gửi khối `usage` cho BE — cả khi lỗi — kèm lý do tính/không tính phí.

Chạy: python manage.py test tests.test_product_image_cost
"""
import base64
import io
import json
import os
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from google.api_core import exceptions as google_api_exceptions
from PIL import Image
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.services import product_image_cost as cost
from video_management.views import product_image_views

# Giá thật của gemini-3.1-flash-image (ai.google.dev/gemini-api/docs/pricing, 2026-09-24).
FLASH_IMAGE_PRICES = json.dumps({'gemini-3.1-flash-image': {'input': 0.5, 'output_text': 3, 'output_image': 60}})


def _usage(prompt, candidates, total):
    return SimpleNamespace(prompt_token_count=prompt, candidates_token_count=candidates, total_token_count=total)


class ReadModelPriceTests(SimpleTestCase):
    def _read(self, raw, model='gemini-3.1-flash-image'):
        with mock.patch.dict(os.environ, {cost.PRICES_ENV: raw}):
            return cost.read_model_price(model)

    def test_thieu_bien_thi_bao_ten_bien(self):
        price, note = self._read('')
        self.assertIsNone(price)
        self.assertIn('GEMINI_IMAGE_PRICES_JSON', note)

    def test_json_hong(self):
        price, note = self._read('{khong phai json')
        self.assertIsNone(price)
        self.assertIn('không phải JSON', note)

    def test_chua_co_gia_cua_model_dang_dung(self):
        """Đổi GEMINI_MODEL mà quên thêm giá → báo đúng model, không lấy giá model khác."""
        price, note = self._read(FLASH_IMAGE_PRICES, model='gemini-3-pro-image')
        self.assertIsNone(price)
        self.assertIn('gemini-3-pro-image', note)

    def test_thieu_khoa_hoac_gia_am(self):
        for entry in ({'input': 0.5, 'output_text': 3}, {'input': -1, 'output_text': 3, 'output_image': 60}):
            price, note = self._read(json.dumps({'m': entry}), model='m')
            self.assertIsNone(price)
            self.assertIn('output_image', note)

    def test_doc_dung_gia(self):
        price, note = self._read(FLASH_IMAGE_PRICES)
        self.assertEqual(price, {'input': 0.5, 'output_text': 3.0, 'output_image': 60.0})
        self.assertIsNone(note)


class SplitTokensAndCostTests(SimpleTestCase):
    PRICE = {'input': 0.5, 'output_text': 3.0, 'output_image': 60.0}

    def test_mot_anh_1K_dung_gia_trang_google(self):
        """Ảnh 1K = 1120 token ≈ $0,067; cộng 2.000 token đầu vào (2 ảnh + prompt) = $0,0682."""
        tokens = cost.split_tokens(_usage(2000, 1120, 3120), [])
        self.assertEqual(tokens, {'input_tokens': 2000, 'thinking_tokens': 0, 'text_tokens': 0, 'image_tokens': 1120})
        self.assertAlmostEqual(cost.cost_usd(tokens, self.PRICE), 0.0682, places=6)

    def test_tach_token_suy_nghi_va_chu_ra_gia_chu(self):
        """total > prompt + candidates → phần dư là suy nghĩ; 40 ký tự chữ ≈ 10 token chữ."""
        tokens = cost.split_tokens(_usage(2000, 1130, 3330), ['x' * 40])
        self.assertEqual(tokens, {'input_tokens': 2000, 'thinking_tokens': 200, 'text_tokens': 10, 'image_tokens': 1120})
        expected = (2000 * 0.5 + (200 + 10) * 3 + 1120 * 60) / 1_000_000
        self.assertAlmostEqual(cost.cost_usd(tokens, self.PRICE), expected, places=9)

    def test_chi_tra_chu_thi_khong_co_token_anh(self):
        tokens = cost.split_tokens(_usage(2000, 25, 2025), ['I cannot edit this image, sorry about that friend.' * 3])
        self.assertEqual(tokens['image_tokens'], 0)
        self.assertEqual(tokens['text_tokens'], 25)  # ước lượng chữ không vượt quá candidates

    def test_usage_tu_phan_hoi(self):
        response = SimpleNamespace(usage_metadata=_usage(2000, 1120, 3120))
        with mock.patch.dict(os.environ, {cost.PRICES_ENV: FLASH_IMAGE_PRICES}):
            usage = cost.usage_from_response('gemini-3.1-flash-image', response, [])
        self.assertEqual(usage, {
            'model': 'gemini-3.1-flash-image', 'input_tokens': 2000, 'output_tokens': 1120,
            'cost_usd': 0.0682, 'cost_note': None,
        })

    def test_thieu_gia_van_ghi_token(self):
        response = SimpleNamespace(usage_metadata=_usage(2000, 1120, 3120))
        with mock.patch.dict(os.environ, {cost.PRICES_ENV: ''}):
            usage = cost.usage_from_response('gemini-3.1-flash-image', response, [])
        self.assertEqual((usage['input_tokens'], usage['output_tokens'], usage['cost_usd']), (2000, 1120, None))
        self.assertIn('GEMINI_IMAGE_PRICES_JSON', usage['cost_note'])

    def test_khong_co_usage_metadata(self):
        usage = cost.usage_from_response('m', SimpleNamespace(), [])
        self.assertIsNone(usage['cost_usd'])
        self.assertIn('không trả số token', usage['cost_note'])


def _png():
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), (0, 128, 0)).save(buffer, format='PNG')
    return buffer.getvalue()


@override_settings(GEMINI_API_KEY='test-key')
class HeldProductUsageInResponseTests(SimpleTestCase):
    """View gửi `usage` cho BE ở mọi nhánh: thành công, Gemini không trả ảnh, lỗi trước khi gọi."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.body = {
            'person_image_base64': base64.b64encode(_png()).decode(),
            'person_mime_type': 'image/png',
            'product_image_base64': base64.b64encode(_png()).decode(),
            'product_mime_type': 'image/png',
        }
        env = mock.patch.dict(os.environ, {'GEMINI_MODEL': 'gemini-3.1-flash-image', cost.PRICES_ENV: FLASH_IMAGE_PRICES})
        env.start()
        self.addCleanup(env.stop)
        configure = mock.patch('google.generativeai.configure')
        configure.start()
        self.addCleanup(configure.stop)

    def _post(self, body=None, response=None, error=None):
        model = mock.Mock()
        if error is not None:
            model.generate_content.side_effect = error
        else:
            model.generate_content.return_value = response
        with mock.patch('google.generativeai.GenerativeModel', return_value=model):
            request = self.factory.post('/api/ai/product-image/held-product/', body or self.body, format='json')
            force_authenticate(request, user=mock.Mock(is_authenticated=True, pk=1))
            return product_image_views.held_product(request)

    def _response(self, parts, usage):
        return SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason='STOP')],
            usage_metadata=usage,
        )

    def test_thanh_cong_kem_chi_phi(self):
        image = SimpleNamespace(inline_data=SimpleNamespace(data=_png(), mime_type='image/png'), text=None)
        res = self._post(response=self._response([image], _usage(2000, 1120, 3120)))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['usage']['cost_usd'], 0.0682)
        self.assertEqual(res.data['usage']['model'], 'gemini-3.1-flash-image')

    def test_khong_tra_anh_van_bao_token_da_ton(self):
        text = SimpleNamespace(inline_data=None, text='I cannot help with that.')
        res = self._post(response=self._response([text], _usage(2000, 8, 2008)))
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.data['usage']['input_tokens'], 2000)
        self.assertGreater(res.data['usage']['cost_usd'], 0)

    def test_model_404_khong_tinh_phi(self):
        res = self._post(error=google_api_exceptions.NotFound('model not found'))
        self.assertEqual(res.data['usage']['cost_usd'], 0.0)

    def test_het_thoi_gian_thi_chi_phi_chua_biet(self):
        res = self._post(error=google_api_exceptions.DeadlineExceeded('timeout'))
        self.assertEqual(res.status_code, 504)
        self.assertIsNone(res.data['usage']['cost_usd'])
        self.assertIn('không biết', res.data['usage']['cost_note'])

    def test_dau_vao_sai_chua_goi_gemini_khong_tinh_phi(self):
        res = self._post(body={**self.body, 'product_image_base64': ''})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['usage']['cost_usd'], 0.0)
