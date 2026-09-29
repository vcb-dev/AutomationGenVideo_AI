"""Tạo ảnh sản phẩm — chế độ "Ghép background": tách nền ảnh SP bằng rembg (0đ).

Khoá phần thuần logic quanh rembg: kiểm ảnh đầu vào, xoay theo EXIF, thu nhỏ ảnh quá to, cắt
sát mép SP (bỏ quầng alpha mờ), báo đúng tên biến khi thiếu REMBG_MODEL, và view bắt buộc JWT.
Bước rembg thật được thay bằng hàm giả trả ảnh RGBA dựng sẵn — chất lượng tách nền của model chỉ
đánh giá được bằng ảnh thật nên đã kiểm bằng tay trên image Dockerfile.railway.

Chạy: python manage.py test tests.test_product_image_cutout
"""
import base64
import io
import os
import sys
import types
from unittest import mock

from django.test import SimpleTestCase
from PIL import Image
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.services import product_image_service as svc
from video_management.views import product_image_views


def _png_bytes(size=(40, 20), color=(200, 30, 30)):
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, format='PNG')
    return buffer.getvalue()


def _fake_remover(box):
    """rembg giả: trả ảnh RGBA trong suốt cùng kích thước, chỉ `box` là SP đục + 1 viền quầng mờ."""
    def remove(image):
        out = Image.new('RGBA', image.size, (0, 0, 0, 0))
        left, top, right, bottom = box
        # Quầng mờ alpha=5 bao quanh SP — rembg thật hay để lại, không được tính vào khung cắt.
        out.paste((255, 255, 255, 5), (max(0, left - 3), max(0, top - 3), right + 3, bottom + 3))
        out.paste((10, 120, 200, 255), box)
        return out
    return remove


class DecodeInputImageTests(SimpleTestCase):
    def test_thieu_anh_bao_dung_ten_anh(self):
        with self.assertRaisesMessage(svc.ProductImageInputError, 'Thiếu ảnh sản phẩm'):
            svc.decode_input_image('', 'image/png', 'ảnh sản phẩm')

    def test_tu_choi_dinh_dang_khong_ho_tro(self):
        encoded = base64.b64encode(b'GIF89a').decode()
        with self.assertRaisesMessage(svc.ProductImageInputError, 'image/gif'):
            svc.decode_input_image(encoded, 'image/gif', 'ảnh sản phẩm')

    def test_tu_choi_base64_hong(self):
        with self.assertRaisesMessage(svc.ProductImageInputError, 'base64 hỏng'):
            svc.decode_input_image('khong-phai-base64!!', 'image/png', 'ảnh sản phẩm')

    def test_tu_choi_anh_qua_lon(self):
        encoded = base64.b64encode(b'x' * 2048).decode()
        with mock.patch.object(svc, 'MAX_INPUT_IMAGE_MB', 0.001):
            with self.assertRaisesMessage(svc.ProductImageInputError, 'quá lớn'):
                svc.decode_input_image(encoded, 'image/png', 'ảnh sản phẩm')

    def test_nhan_webp_va_chuan_hoa_image_jpg(self):
        encoded = base64.b64encode(b'abc').decode()
        self.assertEqual(svc.decode_input_image(encoded, 'IMAGE/WEBP', 'ảnh')[1], 'image/webp')
        self.assertEqual(svc.decode_input_image(encoded, 'image/jpg', 'ảnh')[1], 'image/jpeg')


class TrimAndDownscaleTests(SimpleTestCase):
    def test_cat_sat_mep_bo_qua_quang_mo(self):
        image = _fake_remover((30, 40, 70, 100))(Image.new('RGB', (200, 200)))
        trimmed = svc.trim_to_content(image)
        self.assertEqual(trimmed.size, (40, 60))

    def test_bo_dom_logo_tach_roi_khong_tinh_vao_khung(self):
        """Ảnh catalog thật: đốm mờ còn sót của logo phía trên chiếc nhẫn làm khung cao gấp đôi."""
        image = Image.new('RGBA', (300, 400), (0, 0, 0, 0))
        image.paste((200, 200, 200, 255), (100, 200, 200, 350))  # nhẫn 100x150
        image.paste((200, 200, 200, 40), (140, 20, 146, 24))      # đốm logo 6x4, alpha mờ
        self.assertEqual(svc.trim_to_content(image).size, (100, 150))

    def test_bo_dong_logo_mo_rong_du_dem_diem_anh_vuot_nguong(self):
        """Đúng ca đo thật: dòng logo mờ chiếm >1% SỐ điểm ảnh nhưng alpha thấp → vẫn phải bỏ."""
        image = Image.new('RGBA', (300, 400), (0, 0, 0, 0))
        image.paste((200, 200, 200, 255), (100, 200, 200, 350))  # nhẫn 100x150 = 15.000 điểm
        image.paste((250, 250, 250, 30), (50, 20, 250, 25))       # logo 200x5 = 1.000 điểm (6%)
        self.assertEqual(svc.trim_to_content(image).size, (100, 150))

    def test_giu_ca_hai_mon_tach_roi_co_ngang_nhau(self):
        """Đôi bông tai: hai món cách nhau khoảng trống, món nào cũng đủ lớn → giữ cả hai."""
        image = Image.new('RGBA', (300, 200), (0, 0, 0, 0))
        image.paste((0, 0, 0, 255), (20, 50, 80, 150))
        image.paste((0, 0, 0, 255), (200, 60, 260, 160))
        self.assertEqual(svc.trim_to_content(image).size, (240, 110))

    def test_day_chuyen_manh_lien_dai_voi_mat_day_khong_bi_cat(self):
        image = Image.new('RGBA', (200, 300), (0, 0, 0, 0))
        image.paste((0, 0, 0, 255), (99, 10, 101, 240))   # dây mảnh 2px chạy dọc
        image.paste((0, 0, 0, 255), (80, 240, 120, 290))  # mặt dây
        self.assertEqual(svc.trim_to_content(image).size, (40, 280))

    def test_anh_trong_suot_hoan_toan_thi_bao_khong_thay_san_pham(self):
        empty = Image.new('RGBA', (50, 50), (0, 0, 0, 0))
        with self.assertRaisesMessage(svc.ProductImageInputError, 'Không tìm thấy sản phẩm'):
            svc.trim_to_content(empty)

    def test_thu_nho_giu_ti_le(self):
        resized = svc.downscale_to_max_side(Image.new('RGB', (4000, 1000)), max_side=2000)
        self.assertEqual(resized.size, (2000, 500))

    def test_anh_nho_giu_nguyen_khong_phong_to(self):
        image = Image.new('RGB', (300, 200))
        self.assertIs(svc.downscale_to_max_side(image, max_side=2000), image)


class CutOutProductTests(SimpleTestCase):
    def test_tra_png_da_cat_va_kich_thuoc_sp(self):
        png, width, height = svc.cut_out_product(_png_bytes((120, 80)), _fake_remover((10, 20, 50, 70)))
        self.assertEqual((width, height), (40, 50))
        result = Image.open(io.BytesIO(png))
        self.assertEqual(result.format, 'PNG')
        self.assertEqual(result.mode, 'RGBA')
        self.assertEqual(result.size, (40, 50))

    def test_xoay_theo_exif_truoc_khi_tach_nen(self):
        """Ảnh chụp điện thoại lưu 200x100 kèm cờ xoay 90° — rembg phải nhận ảnh đứng 100x200."""
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: xoay 90° theo chiều kim đồng hồ khi hiển thị
        buffer = io.BytesIO()
        Image.new('RGB', (200, 100), (255, 255, 255)).save(buffer, format='JPEG', exif=exif)
        seen = {}

        def remover(image):
            seen['size'] = image.size
            return _fake_remover((0, 0, 10, 10))(image)

        svc.cut_out_product(buffer.getvalue(), remover)
        self.assertEqual(seen['size'], (100, 200))

    def test_file_khong_phai_anh(self):
        with self.assertRaisesMessage(svc.ProductImageInputError, 'không phải file ảnh'):
            svc.cut_out_product(b'not an image', _fake_remover((0, 0, 1, 1)))


class RembgSessionTests(SimpleTestCase):
    def setUp(self):
        svc._rembg_sessions.clear()
        self.addCleanup(svc._rembg_sessions.clear)
        self.new_session = mock.Mock(side_effect=lambda name, sess_opts: object())
        fake_rembg = types.ModuleType('rembg')
        fake_rembg.new_session = self.new_session
        fake_ort = types.ModuleType('onnxruntime')
        fake_ort.SessionOptions = types.SimpleNamespace
        patcher = mock.patch.dict(sys.modules, {'rembg': fake_rembg, 'onnxruntime': fake_ort})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tat_arena_de_khong_giu_2GB_ram_moi_worker(self):
        svc.get_rembg_session('isnet-general-use')
        options = self.new_session.call_args.kwargs['sess_opts']
        self.assertFalse(options.enable_cpu_mem_arena)
        self.assertFalse(options.enable_mem_pattern)

    def test_giu_lai_session_theo_ten_model(self):
        first = svc.get_rembg_session('isnet-general-use')
        self.assertIs(svc.get_rembg_session('isnet-general-use'), first)
        self.assertEqual(self.new_session.call_count, 1)

    def test_ten_model_sai_bao_sua_rembg_model(self):
        self.new_session.side_effect = ValueError("No session class found for model 'abc'")
        with self.assertRaisesMessage(svc.ProductImageConfigError, 'REMBG_MODEL=abc'):
            svc.get_rembg_session('abc')


class ReadRembgModelTests(SimpleTestCase):
    def test_thieu_bien_thi_bao_ten_bien(self):
        with mock.patch.dict(os.environ, {'REMBG_MODEL': ''}):
            with self.assertRaisesMessage(svc.ProductImageConfigError, 'REMBG_MODEL'):
                svc.read_rembg_model()

    def test_doc_dung_gia_tri(self):
        with mock.patch.dict(os.environ, {'REMBG_MODEL': ' isnet-general-use '}):
            self.assertEqual(svc.read_rembg_model(), 'isnet-general-use')


class CutoutViewTests(SimpleTestCase):
    URL = '/api/ai/product-image/cutout/'

    def setUp(self):
        self.factory = APIRequestFactory()
        self.body = {
            'image_base64': base64.b64encode(_png_bytes((60, 60))).decode(),
            'mime_type': 'image/png',
        }
        # rembg thật không cần cho test view: thay module bằng bản giả có remove() dùng session giả.
        fake_rembg = types.ModuleType('rembg')
        fake_rembg.remove = lambda image, session=None: session(image)
        self.rembg_patch = mock.patch.dict(sys.modules, {'rembg': fake_rembg})
        self.rembg_patch.start()
        self.addCleanup(self.rembg_patch.stop)

    def _post(self, body, authenticated=True):
        request = self.factory.post(self.URL, body, format='json')
        if authenticated:
            force_authenticate(request, user=mock.Mock(is_authenticated=True, pk=1))
        return product_image_views.cutout(request)

    def test_chua_dang_nhap_bi_chan(self):
        self.assertEqual(self._post(self.body, authenticated=False).status_code, 403)

    def test_thieu_rembg_model_tra_500_neu_ten_bien(self):
        with mock.patch.dict(os.environ, {'REMBG_MODEL': ''}):
            response = self._post(self.body)
        self.assertEqual(response.status_code, 500)
        self.assertIn('REMBG_MODEL', response.data['error_message'])

    def test_sai_dinh_dang_tra_400(self):
        response = self._post({**self.body, 'mime_type': 'image/gif'})
        self.assertEqual(response.status_code, 400)

    def test_thanh_cong_tra_png_va_kich_thuoc(self):
        with mock.patch.dict(os.environ, {'REMBG_MODEL': 'isnet-general-use'}), \
                mock.patch.object(svc, 'get_rembg_session', return_value=_fake_remover((5, 10, 25, 50))) as get_session:
            response = self._post(self.body)
        self.assertEqual(response.status_code, 200, response.data)
        get_session.assert_called_once_with('isnet-general-use')
        self.assertEqual((response.data['width'], response.data['height']), (20, 40))
        png = Image.open(io.BytesIO(base64.b64decode(response.data['cutout_image_base64'])))
        self.assertEqual(png.size, (20, 40))
