"""Tạo ảnh sản phẩm — chế độ "Đặt SP vào cảnh" (tạo hàng loạt): Gemini lấy SP ở ảnh 1, đặt vào cảnh
ở ảnh 2 (tính phí).

Khoá contract: thứ tự ảnh SP trước, cảnh sau (prompt gọi "image 1"/"image 2" theo thứ tự này), prompt
giữ nguyên văn người dùng viết + ghi chú gắn cuối, thiếu ảnh nào báo đúng ảnh đó trước khi gọi Gemini,
mọi phản hồi đã chạm tới Gemini đều kèm `usage` để BE ghi chi phí, và view bắt buộc JWT. SDK Gemini
được thay bằng bản giả — chất lượng ảnh thật chỉ đánh giá được bằng tay với khoá có billing.

Chạy: python manage.py test tests.test_product_image_scene_placement
"""
import base64
import os
from unittest import mock

from django.test import SimpleTestCase, override_settings
from google.api_core import exceptions as google_api_exceptions
from rest_framework.test import APIRequestFactory, force_authenticate

from tests.test_product_image_held_product import _gemini_response, _image_bytes, _image_part, _text_part
from video_management.services import product_image_service as svc
from video_management.views import product_image_views


class BuildScenePlacementPromptTests(SimpleTestCase):
    def test_khong_ghi_chu_thi_dung_prompt_goc(self):
        self.assertEqual(svc.build_scene_placement_prompt(None), svc.SCENE_PLACEMENT_PROMPT)
        self.assertEqual(svc.build_scene_placement_prompt('  '), svc.SCENE_PLACEMENT_PROMPT)

    def test_ghi_chu_gan_cuoi_prompt(self):
        prompt = svc.build_scene_placement_prompt(' túi cao khoảng 25cm ')
        self.assertTrue(prompt.startswith(svc.SCENE_PLACEMENT_PROMPT))
        self.assertTrue(prompt.endswith('túi cao khoảng 25cm'))

    def test_ghi_chu_qua_dai_bi_chan(self):
        with self.assertRaisesMessage(svc.ProductImageInputError, 'tối đa'):
            svc.build_scene_placement_prompt('a' * (svc.HELD_PRODUCT_NOTE_MAX_CHARS + 1))

    def test_prompt_goi_ten_anh_theo_thu_tu(self):
        self.assertTrue(svc.SCENE_PLACEMENT_PROMPT.startswith('Image 1 may contain the handbag'))
        self.assertIn('into the scene of image 2', svc.SCENE_PLACEMENT_PROMPT)
        self.assertIn('Keep the scene of image 2 unchanged.', svc.SCENE_PLACEMENT_PROMPT)

    def test_prompt_cham_chi_giu_nguyen_tui_va_ty_le_khung(self):
        self.assertIn('Keep the handbag 100% identical to image 1', svc.SCENE_PLACEMENT_PROMPT)
        self.assertIn('Bag fills about 45-55% of the frame height', svc.SCENE_PLACEMENT_PROMPT)


@override_settings(GEMINI_API_KEY='test-key')
class ScenePlacementViewTests(SimpleTestCase):
    URL = '/api/ai/product-image/scene-placement/'

    def setUp(self):
        self.factory = APIRequestFactory()
        self.product = _image_bytes('PNG', (10, 12))
        self.scene = _image_bytes('JPEG', (30, 40))
        self.body = {
            'product_image_base64': base64.b64encode(self.product).decode(),
            'product_mime_type': 'image/png',
            'scene_image_base64': base64.b64encode(self.scene).decode(),
            'scene_mime_type': 'image/jpeg',
            'note': 'túi cao 25cm',
        }
        env_patch = mock.patch.dict(os.environ, {'GEMINI_MODEL': 'gemini-image-test'})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        configure_patch = mock.patch('google.generativeai.configure')
        configure_patch.start()
        self.addCleanup(configure_patch.stop)

    def _post(self, body, authenticated=True):
        request = self.factory.post(self.URL, body, format='json')
        if authenticated:
            force_authenticate(request, user=mock.Mock(is_authenticated=True, pk=1))
        return product_image_views.scene_placement(request)

    def _mock_model(self, response=None, error=None):
        model = mock.Mock()
        if error is not None:
            model.generate_content.side_effect = error
        else:
            model.generate_content.return_value = response
        patcher = mock.patch('google.generativeai.GenerativeModel', return_value=model)
        constructor = patcher.start()
        self.addCleanup(patcher.stop)
        return model, constructor

    def test_chua_dang_nhap_bi_chan(self):
        self.assertEqual(self._post(self.body, authenticated=False).status_code, 403)

    def test_thieu_anh_canh_tra_400_neu_dung_anh_va_chua_tinh_phi(self):
        model, _ = self._mock_model()
        response = self._post({**self.body, 'scene_image_base64': ''})
        self.assertEqual(response.status_code, 400)
        self.assertIn('ảnh cảnh', response.data['error_message'])
        self.assertEqual(response.data['usage']['cost_usd'], 0)
        model.generate_content.assert_not_called()

    def test_thieu_anh_san_pham_tra_400(self):
        response = self._post({**self.body, 'product_image_base64': ''})
        self.assertEqual(response.status_code, 400)
        self.assertIn('ảnh sản phẩm', response.data['error_message'])

    def test_thanh_cong_gui_san_pham_truoc_canh_sau_va_tra_usage(self):
        output = _image_bytes('JPEG', (30, 40))
        model, constructor = self._mock_model(_gemini_response([_image_part(output, 'image/jpeg')]))

        response = self._post(self.body)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['mime_type'], 'image/jpeg')
        self.assertEqual(base64.b64decode(response.data['image_base64']), output)
        self.assertEqual(response.data['usage']['model'], 'gemini-image-test')
        constructor.assert_called_once_with('gemini-image-test')
        contents = model.generate_content.call_args.args[0]
        self.assertEqual(contents[0], {'mime_type': 'image/png', 'data': self.product})
        self.assertEqual(contents[1], {'mime_type': 'image/jpeg', 'data': self.scene})
        self.assertTrue(contents[2].startswith(svc.SCENE_PLACEMENT_PROMPT))
        self.assertTrue(contents[2].endswith('túi cao 25cm'))

    def test_gemini_chi_tra_chu_thi_502_kem_loi_gemini_va_usage(self):
        self._mock_model(_gemini_response([_text_part('I cannot place this bag.')], finish_reason='SAFETY'))
        response = self._post(self.body)
        self.assertEqual(response.status_code, 502)
        self.assertIn('I cannot place this bag.', response.data['error_message'])
        self.assertIn('usage', response.data)

    def test_model_404_bao_sua_gemini_model(self):
        self._mock_model(error=google_api_exceptions.NotFound('model not found'))
        response = self._post(self.body)
        self.assertEqual(response.status_code, 500)
        self.assertIn('GEMINI_MODEL=gemini-image-test', response.data['error_message'])

    def test_het_quota_tra_503(self):
        self._mock_model(error=google_api_exceptions.ResourceExhausted('quota'))
        self.assertEqual(self._post(self.body).status_code, 503)
