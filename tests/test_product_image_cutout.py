"""Tạo ảnh sản phẩm — chế độ "Ghép background": tách nền ảnh SP bằng rembg + model tự huấn luyện (0đ).

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
import tempfile
import types
from unittest import mock

# Nạp scipy.ndimage TRƯỚC các mock.patch.dict(sys.modules, ...) bên dưới: patch.dict khôi phục sys.modules bằng cách
# gỡ mọi module nạp trong lúc patch → lần sau import lại phần mở rộng C của scipy thì báo "cannot load module more
# than once per process" (hỏng cả test chạy sau, 13/10).
import scipy.ndimage  # noqa: F401
from django.test import SimpleTestCase, override_settings
from PIL import Image
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.services import product_detector as det
from video_management.services import product_image_service as svc
from video_management.services import product_upscale as up
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
        png, width, height, body_box = svc.cut_out_product(_png_bytes((120, 80)), _fake_remover((10, 20, 50, 70)))
        self.assertEqual((width, height), (40, 50))
        result = Image.open(io.BytesIO(png))
        self.assertEqual(result.format, 'PNG')
        self.assertEqual(result.mode, 'RGBA')
        self.assertEqual(result.size, (40, 50))

    def test_anh_nen_trong_suot_dat_len_nen_trang_va_giu_vung_trong_suot(self):
        """WebP/PNG đã tách nền sẵn: màu dưới vùng trong suốt là rác (sọc) — rembg phải thấy nền TRẮNG, và vùng trong
        suốt của ảnh gốc vẫn trong suốt dù rembg giữ nhầm."""
        rgba = Image.new('RGBA', (60, 40), (0, 0, 0, 0))
        stripes = rgba.load()
        for y in range(40):
            for x in range(60):
                stripes[x, y] = (200, 120, 40, 0) if y % 2 else (90, 60, 20, 0)   # màu rác, alpha 0
        rgba.paste((150, 90, 50, 255), (20, 10, 40, 30))                         # SP thật
        buffer = io.BytesIO()
        rgba.save(buffer, format='WEBP', lossless=True, exact=True)
        seen = {}

        def keep_everything(image):
            seen['image'] = image.copy()
            return Image.new('RGBA', image.size, (128, 128, 128, 255))            # rembg giữ nhầm cả khung

        png, width, height, _ = svc.cut_out_product(buffer.getvalue(), keep_everything)
        self.assertEqual(seen['image'].mode, 'RGB')
        self.assertEqual(seen['image'].getpixel((2, 1)), (255, 255, 255))         # nền trắng, không còn sọc
        self.assertEqual(seen['image'].getpixel((30, 20)), (150, 90, 50))
        self.assertEqual((width, height), (20, 20))                               # chỉ còn SP thật

    def test_khung_than_lo_ra_vung_trong_suot_khong_thanh_mang_trang(self):
        """Khung thân model rộng hơn thân thật (lố ra khe cạnh dây): làm đặc thân không được biến khe trong suốt của ảnh
        gốc thành mảng trắng (gặp 15/10, ảnh WebP túi Nodde)."""
        rgba = Image.new('RGBA', (60, 40), (0, 0, 0, 0))
        rgba.paste((150, 90, 50, 255), (20, 10, 40, 30))
        buffer = io.BytesIO()
        rgba.save(buffer, format='PNG')
        locate = lambda image, alpha: ((10, 5, 50, 35), [], (12, 5, 48, 35))
        png, width, height, _ = svc.cut_out_product(
            buffer.getvalue(), lambda image: Image.new('RGBA', image.size, (128, 128, 128, 255)), locate)
        self.assertEqual((width, height), (20, 20))
        result = Image.open(io.BytesIO(png))
        self.assertEqual(result.getpixel((0, 0))[3], 255)

    def test_anh_png_dac_hoan_toan_khong_bi_coi_la_trong_suot(self):
        self.assertIsNone(svc.transparency_of(Image.new('RGBA', (4, 4), (10, 20, 30, 255))))
        self.assertIsNone(svc.transparency_of(Image.new('RGB', (4, 4))))
        self.assertIsNotNone(svc.transparency_of(Image.new('RGBA', (4, 4), (10, 20, 30, 0))))

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

    def test_bao_sp_cham_mep_anh_goc_va_co_sp_theo_diem_anh_goc(self):
        """Ảnh gốc cắt mất nửa phải túi (SP chạm mép phải) → báo mép phải; cỡ SP tính theo điểm ảnh GỐC."""
        info = {}
        svc.cut_out_product(_png_bytes((120, 80)), _fake_remover((60, 20, 120, 70)), info=info)
        self.assertEqual(info['cut_off_sides'], ['right'])
        self.assertEqual(info['product_source_px'], 60)

    def test_co_sp_quy_ve_anh_goc_khi_da_phong(self):
        """Ảnh nhỏ phóng ×2 trước khi tách nền: SP 80×100 trên ảnh phóng = 50px trên ảnh gốc."""
        info = {}
        upscale = lambda image: (image.resize((image.width * 2, image.height * 2)), 2)
        svc.cut_out_product(_png_bytes((120, 80)), _fake_remover((20, 40, 100, 140)), upscale=upscale, info=info)
        self.assertEqual(info['cut_off_sides'], [])
        self.assertEqual(info['product_source_px'], 50)


class InputQualityWarningTests(SimpleTestCase):
    def test_sp_lot_trong_khung_va_du_lon_thi_khong_bao(self):
        self.assertEqual(svc.input_quality_warnings({'cut_off_sides': [], 'product_source_px': 1200}), [])

    def test_bao_mep_bi_cat_bang_tieng_viet(self):
        warnings = svc.input_quality_warnings({'cut_off_sides': ['right', 'bottom'], 'product_source_px': 1200})
        self.assertEqual(len(warnings), 1)
        self.assertIn('mép phải, dưới', warnings[0])

    def test_bao_tui_qua_nho_kem_so_diem_anh(self):
        warnings = svc.input_quality_warnings({'cut_off_sides': [], 'product_source_px': 180, 'source_size': [192, 256]})
        self.assertEqual(len(warnings), 1)
        self.assertIn('ảnh 192×256px, túi chỉ ~180px', warnings[0])
        self.assertIn('Google Hình ảnh', warnings[0])                 # chỉ cách lấy bản lớn khi lưu ảnh trên mạng
        self.assertIn(f'{svc.SHARP_PRODUCT_MIN_SIDE_PX}px', warnings[0])

    def test_khong_co_thong_tin_thi_khong_bao(self):
        self.assertEqual(svc.input_quality_warnings({}), [])


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


def _view(bag, body=None, stand=None):
    return {'bag': [bag] if bag else [], 'bag_body': [body] if body else [], 'stand': [stand] if stand else [],
            'other_bag': [], 'prop': []}


def _bag_with_leg_and_jar(image):
    """rembg giả: "túi" 5..25 × 10..40, "chân giá" dính liền đáy tới y=55, "lọ" tách rời ở góc."""
    out = Image.new('RGBA', image.size, (0, 0, 0, 0))
    out.paste((10, 120, 200, 255), (5, 10, 25, 40))
    out.paste((40, 40, 40, 255), (13, 40, 17, 55))
    out.paste((200, 200, 200, 255), (45, 45, 55, 55))
    return out


class CutoutViewTests(SimpleTestCase):
    """Tách nền 0đ: chỉ model tự huấn luyện, không gọi Gemini (không cần GEMINI_API_KEY)."""
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
        model_file = tempfile.NamedTemporaryFile(suffix='.onnx')
        self.addCleanup(model_file.close)
        env = mock.patch.dict(os.environ, {'REMBG_MODEL': 'isnet-general-use', 'PRODUCT_IMAGE_DETECTOR_MODEL_PATH': model_file.name,
                                           'PRODUCT_IMAGE_UPSCALE_MODEL_PATH': model_file.name})
        env.start()
        self.addCleanup(env.stop)
        # Model phóng ảnh giả: giữ nguyên ảnh (khung giả của rembg tính theo ảnh 60px) — đếm số lần gọi.
        get_upscaler = mock.patch.object(up, 'get_upscaler', return_value=mock.Mock(name='upscaler'))
        get_upscaler.start()
        self.addCleanup(get_upscaler.stop)
        upscale = mock.patch.object(up, 'upscale_if_small', side_effect=lambda session, image: (image, 1))
        self.upscale = upscale.start()
        self.addCleanup(upscale.stop)
        get_detector = mock.patch.object(det, 'get_detector', return_value=mock.Mock(name='detector'))
        get_detector.start().return_value.name = 'bag-detector-test'
        self.addCleanup(get_detector.stop)
        # Model giả mặc định: tự tin, túi chiếm cả ảnh.
        detect_product = mock.patch.object(det, 'detect_product', return_value=det.DetectionResult(_view((0, 0, 60, 60, 0.95)), {}, []))
        self.detect_product = detect_product.start()
        self.addCleanup(detect_product.stop)

    def _post(self, body, authenticated=True, remover=None):
        request = self.factory.post(self.URL, body, format='json')
        if authenticated:
            force_authenticate(request, user=mock.Mock(is_authenticated=True, pk=1))
        with mock.patch.object(svc, 'get_rembg_session', return_value=remover or _fake_remover((5, 10, 25, 50))):
            return product_image_views.cutout(request)

    def test_chua_dang_nhap_bi_chan(self):
        self.assertEqual(self._post(self.body, authenticated=False).status_code, 403)

    def test_thieu_rembg_model_tra_500_neu_ten_bien(self):
        with mock.patch.dict(os.environ, {'REMBG_MODEL': ''}):
            response = self._post(self.body)
        self.assertEqual(response.status_code, 500)
        self.assertIn('REMBG_MODEL', response.data['error_message'])

    def test_thieu_bien_model_tu_huan_luyen_tra_500_neu_ten_bien(self):
        with mock.patch.dict(os.environ, {'PRODUCT_IMAGE_DETECTOR_MODEL_PATH': ''}):
            response = self._post(self.body)
        self.assertEqual(response.status_code, 500)
        self.assertIn('PRODUCT_IMAGE_DETECTOR_MODEL_PATH', response.data['error_message'])
        self.detect_product.assert_not_called()

    def test_thieu_bien_model_phong_anh_tra_500_neu_ten_bien(self):
        with mock.patch.dict(os.environ, {'PRODUCT_IMAGE_UPSCALE_MODEL_PATH': ''}), \
                mock.patch.object(up, 'get_upscaler', side_effect=AssertionError('không được nạp model khi thiếu biến')):
            response = self._post(self.body)
        self.assertEqual(response.status_code, 500)
        self.assertIn('PRODUCT_IMAGE_UPSCALE_MODEL_PATH', response.data['error_message'])

    def test_phong_anh_truoc_khi_tach_nen_va_tra_co_anh_goc(self):
        response = self._post(self.body)
        self.assertEqual(response.status_code, 200, response.data)
        self.upscale.assert_called_once()
        self.assertEqual(self.upscale.call_args[0][1].size, (60, 60))   # ảnh gốc đưa vào bước phóng
        self.assertEqual(response.data['source_size'], [60, 60])
        self.assertEqual(response.data['upscaled'], 1)

    def test_bao_anh_goc_khong_du_lam_anh_thuong_mai(self):
        """Túi giả 20×40px trong ảnh 60px — quá nhỏ để ghép nét: vẫn ghép (200) nhưng trả câu báo cho FE."""
        response = self._post(self.body)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data['quality_warnings']), 1)
        self.assertIn('~40px', response.data['quality_warnings'][0])

    def test_sai_dinh_dang_tra_400(self):
        response = self._post({**self.body, 'mime_type': 'image/gif'})
        self.assertEqual(response.status_code, 400)

    @override_settings(GEMINI_API_KEY='')
    def test_thanh_cong_0d_khong_can_khoa_gemini(self):
        response = self._post(self.body)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['width'], response.data['height']), (20, 40))
        png = Image.open(io.BytesIO(base64.b64decode(response.data['cutout_image_base64'])))
        self.assertEqual(png.size, (20, 40))
        self.assertEqual(response.data['usage'],
                         {'model': 'bag-detector-test', 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0, 'cost_note': None})
        self.assertEqual(response.data['detection'], {'status': 'applied', 'source': 'model', 'props_removed': 0})
        # Model nhận đúng ảnh đã thu nhỏ + mặt nạ rembg cùng cỡ.
        image, alpha = self.detect_product.call_args.args[1:]
        self.assertEqual(alpha.shape, (image.height, image.width))

    def test_model_tu_tin_bo_chan_gia_va_vat_tach_roi(self):
        main = _view((5, 10, 25, 40, 0.95), body=(5, 20, 25, 40, 0.95), stand=(12, 40, 18, 56, 0.9))
        self.detect_product.return_value = det.DetectionResult(main, {'bag': 0.97}, [])
        response = self._post(self.body, remover=_bag_with_leg_and_jar)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['width'], response.data['height']), (20, 30))
        self.assertEqual(response.data['detection'], {'status': 'applied', 'source': 'model', 'props_removed': 1})
        self.assertEqual(response.data['body_box'], [0, 10, 20, 30])

    def test_model_chua_chac_chi_bo_vat_tach_roi_va_bao_phan_dinh_lien(self):
        # Khung lệch giữa 3 lần → không dám cắt chân giá dính liền túi (có thể cắt nhầm túi), chỉ bỏ cái lọ
        # tách rời; chân giá model nghi là kệ chiếm >2% ảnh SP → báo người dùng xem lại.
        reasons = ['khung túi 3 lần lệch nhau (60%)']
        main = _view((5, 10, 25, 40, 0.9), body=(5, 20, 25, 40, 0.9), stand=(12, 40, 18, 56, 0.9))
        self.detect_product.return_value = det.DetectionResult(main, {'bag': 0.6}, reasons)
        response = self._post(self.body, remover=_bag_with_leg_and_jar)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['width'], response.data['height']), (20, 45))   # túi + chân giá, không có lọ
        detection = response.data['detection']
        self.assertEqual((detection['status'], detection['source'], detection['model_reasons']), ('skipped', 'model', reasons))
        self.assertIn('chưa cắt phần dính liền túi', detection['reason'])
        self.assertEqual(response.data['usage']['cost_usd'], 0.0)

    def test_model_chua_chac_ma_khong_co_gi_dinh_lien_thi_khong_canh_bao(self):
        reasons = ['kệ/hộp lúc thấy lúc không']
        main = _view((5, 10, 25, 50, 0.9), body=(5, 20, 25, 50, 0.9))
        self.detect_product.return_value = det.DetectionResult(main, {}, reasons)
        response = self._post(self.body)
        self.assertEqual(response.data['detection'],
                         {'status': 'applied', 'source': 'model', 'props_removed': 0, 'model_reasons': reasons})

    def test_khong_thay_tui_giu_nguyen_anh_tach_nen_va_bao(self):
        self.detect_product.return_value = det.DetectionResult(_view(None), {}, ['không thấy túi'])
        response = self._post(self.body, remover=_bag_with_leg_and_jar)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['width'], response.data['height']), (50, 45))   # giữ cả túi, chân giá, lọ
        self.assertEqual(response.data['detection']['status'], 'skipped')
        self.assertIn('không thấy túi', response.data['detection']['reason'])
