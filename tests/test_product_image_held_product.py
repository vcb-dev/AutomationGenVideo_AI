"""Tạo ảnh sản phẩm — chế độ "Chị Nhạm cầm SP": Gemini thay SP cũ trên tay bằng SP mới (tính phí).

Khoá contract quanh lượt gọi Gemini: đúng thứ tự ảnh (ảnh chị Nhạm trước, SP mới sau — prompt
gọi tên theo thứ tự này), ghi chú người dùng gắn cuối prompt và bị chặn độ dài, lấy mime ảnh từ
phản hồi chứ không cố định PNG, không có ảnh thì đưa nguyên lời Gemini vào thông báo lỗi, model
404 thì báo sửa GEMINI_MODEL, và view bắt buộc JWT. SDK Gemini được thay bằng bản giả — chất
lượng ảnh thật chỉ đánh giá được bằng tay với khoá có billing.

Chạy: python manage.py test tests.test_product_image_held_product
"""
import base64
import io
import os
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from google.api_core import exceptions as google_api_exceptions
from PIL import Image
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.services import product_image_service as svc
from video_management.views import product_image_views


def _image_bytes(image_format='PNG', size=(8, 8)):
    buffer = io.BytesIO()
    Image.new('RGB', size, (0, 128, 0)).save(buffer, format=image_format)
    return buffer.getvalue()


def _gemini_response(parts, finish_reason='STOP'):
    return SimpleNamespace(candidates=[
        SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason=finish_reason)
    ])


def _image_part(data, mime_type):
    return SimpleNamespace(inline_data=SimpleNamespace(data=data, mime_type=mime_type), text=None)


def _text_part(text):
    return SimpleNamespace(inline_data=None, text=text)


class BuildPromptTests(SimpleTestCase):
    def test_khong_ghi_chu_thi_dung_prompt_goc(self):
        self.assertEqual(svc.build_held_product_prompt(None), svc.HELD_PRODUCT_PROMPT)
        self.assertEqual(svc.build_held_product_prompt('   '), svc.HELD_PRODUCT_PROMPT)

    def test_ghi_chu_gan_cuoi_prompt(self):
        prompt = svc.build_held_product_prompt('  hộp cao khoảng 30cm ')
        self.assertTrue(prompt.startswith(svc.HELD_PRODUCT_PROMPT))
        self.assertTrue(prompt.endswith('hộp cao khoảng 30cm'))

    def test_ghi_chu_qua_dai_bi_chan(self):
        with self.assertRaisesMessage(svc.ProductImageInputError, 'tối đa'):
            svc.build_held_product_prompt('a' * (svc.HELD_PRODUCT_NOTE_MAX_CHARS + 1))

    def test_ghi_chu_khong_phai_chuoi_bi_bo_qua(self):
        self.assertEqual(svc.build_held_product_prompt(123), svc.HELD_PRODUCT_PROMPT)

    def test_prompt_goi_ten_anh_theo_thu_tu(self):
        self.assertIn('IMAGE 1 is the base photo', svc.HELD_PRODUCT_PROMPT)
        self.assertIn('IMAGE 2 is a NEW product', svc.HELD_PRODUCT_PROMPT)


class ExtractGeneratedImageTests(SimpleTestCase):
    def test_lay_anh_dau_tien_va_mime_tu_phan_hoi(self):
        jpeg = _image_bytes('JPEG')
        response = _gemini_response([_text_part('Here you go'), _image_part(jpeg, 'image/jpeg')])
        image, mime, texts, finish = svc.extract_generated_image(response)
        self.assertEqual((image, mime, texts, finish), (jpeg, 'image/jpeg', ['Here you go'], 'STOP'))

    def test_thieu_mime_thi_doc_tu_noi_dung_file(self):
        png = _image_bytes('PNG')
        _, mime, _, _ = svc.extract_generated_image(_gemini_response([_image_part(png, '')]))
        self.assertEqual(mime, 'image/png')

    def test_chi_co_chu_thi_khong_co_anh(self):
        image, mime, texts, _ = svc.extract_generated_image(_gemini_response([_text_part('I cannot')]))
        self.assertEqual((image, mime, texts), (None, None, ['I cannot']))

    def test_khong_co_candidate(self):
        self.assertEqual(svc.extract_generated_image(SimpleNamespace(candidates=[])), (None, None, [], None))


class ReadGeminiModelTests(SimpleTestCase):
    def test_thieu_bien_thi_bao_ten_bien(self):
        with mock.patch.dict(os.environ, {'GEMINI_MODEL': ''}):
            with self.assertRaisesMessage(svc.ProductImageConfigError, 'GEMINI_MODEL'):
                svc.read_gemini_image_model()


@override_settings(GEMINI_API_KEY='test-key')
class HeldProductViewTests(SimpleTestCase):
    URL = '/api/ai/product-image/held-product/'

    def setUp(self):
        self.factory = APIRequestFactory()
        self.person = _image_bytes('JPEG', (30, 40))
        self.product = _image_bytes('PNG', (10, 10))
        self.body = {
            'person_image_base64': base64.b64encode(self.person).decode(),
            'person_mime_type': 'image/jpeg',
            'product_image_base64': base64.b64encode(self.product).decode(),
            'product_mime_type': 'image/png',
            'note': 'chai cao 20cm',
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
        return product_image_views.held_product(request)

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

    def test_thieu_anh_san_pham_moi_tra_400_neu_dung_anh(self):
        body = {**self.body, 'product_image_base64': ''}
        response = self._post(body)
        self.assertEqual(response.status_code, 400)
        self.assertIn('ảnh sản phẩm mới', response.data['error_message'])

    def test_thanh_cong_gui_dung_thu_tu_anh_va_tra_mime_that(self):
        output = _image_bytes('JPEG', (30, 40))
        model, constructor = self._mock_model(_gemini_response([_image_part(output, 'image/jpeg')]))

        response = self._post(self.body)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['mime_type'], 'image/jpeg')
        self.assertEqual(base64.b64decode(response.data['image_base64']), output)
        constructor.assert_called_once_with('gemini-image-test')
        contents = model.generate_content.call_args.args[0]
        self.assertEqual(contents[0], {'mime_type': 'image/jpeg', 'data': self.person})
        self.assertEqual(contents[1], {'mime_type': 'image/png', 'data': self.product})
        self.assertTrue(contents[2].endswith('chai cao 20cm'))

    def test_gemini_chi_tra_chu_thi_502_kem_loi_gemini(self):
        self._mock_model(_gemini_response([_text_part('I cannot edit this image.')], finish_reason='SAFETY'))
        response = self._post(self.body)
        self.assertEqual(response.status_code, 502)
        self.assertIn('I cannot edit this image.', response.data['error_message'])
        self.assertIn('SAFETY', response.data['error_message'])

    def test_model_404_bao_sua_gemini_model(self):
        self._mock_model(error=google_api_exceptions.NotFound('model not found'))
        response = self._post(self.body)
        self.assertEqual(response.status_code, 500)
        self.assertIn('GEMINI_MODEL=gemini-image-test', response.data['error_message'])

    def test_het_quota_tra_503(self):
        self._mock_model(error=google_api_exceptions.ResourceExhausted('quota'))
        self.assertEqual(self._post(self.body).status_code, 503)

    @override_settings(GEMINI_API_KEY='')
    def test_thieu_api_key_tra_500(self):
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': ''}):
            response = self._post(self.body)
        self.assertEqual(response.status_code, 500)
        self.assertIn('GEMINI_API_KEY', response.data['error_message'])
