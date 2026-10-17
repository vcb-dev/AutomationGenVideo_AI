"""Tạo ảnh sản phẩm — lọc đúng SP khi tách nền: bỏ giá trưng bày, đạo cụ; làm đặc thân túi màu sáng.

rembg giữ mọi vật nổi bật (đo trên ảnh thử: túi trên giá trưng bày thì 25% điểm "túi" là giá) — model
nhận diện tự huấn luyện trả khung SP + khung từng kệ/đạo cụ (test riêng ở test_product_image_detector),
`isolate_product` chỉ giữ phần thuộc SP. Khoá: bỏ mọi thứ ngoài khung SP và mẩu đạo cụ trong dải nới, xoá
thanh giá lộ trong khe quai nhưng giữ quai vắt ngang và thân túi, không đụng đạo cụ to; túi kem/trắng mà
rembg để nửa trong suốt thì thân được làm đặc nhưng lỗ thật (khe quai) và viền mềm giữ nguyên.

Chạy: python manage.py test tests.test_product_image_product_detection
"""
import io

import numpy as np
from django.test import SimpleTestCase
from PIL import Image

from video_management.services import product_image_service as svc


def _bag_on_stand():
    """Túi 200×200 (thân y 100..180, quai y 40..100) trên giá: thanh đứng x 98..102 xuyên từ y 0 tới
    y 195, chân giá y 180..195. Trả (alpha, khung túi, [khung đạo cụ])."""
    alpha = np.zeros((200, 200), np.uint8)
    alpha[100:180, 40:160] = 255                       # thân túi
    alpha[40:50, 60:140] = 255                         # quai: thanh ngang trên cùng
    alpha[40:100, 60:68] = 255                         # quai: cột trái
    alpha[40:100, 132:140] = 255                       # quai: cột phải
    alpha[0:195, 98:102] = 255                         # thanh đứng của giá (lộ trong khe quai)
    alpha[180:195, 70:130] = 255                       # chân/đế giá dưới đáy túi
    product = (40, 40, 160, 180)
    props = [(98, 0, 102, 195), (70, 180, 130, 195)]
    return alpha, product, props


class IsolateProductTests(SimpleTestCase):
    def setUp(self):
        self.alpha, self.product, self.props = _bag_on_stand()
        self.out = svc.isolate_product(self.alpha, self.product, self.props)

    def test_bo_chan_de_va_thanh_tren_dinh_ngoai_khung_san_pham(self):
        self.assertEqual(self.out[185:, :].max(), 0)   # chân giá dưới đáy
        self.assertEqual(self.out[:30, :].max(), 0)    # thanh giá nhô trên quai

    def test_bo_manh_chan_gia_trong_dai_noi_sat_day_tui(self):
        self.assertEqual(self.out[180:185, 70:130].max(), 0)

    def test_xoa_thanh_gia_lo_trong_khe_quai(self):
        self.assertEqual(self.out[55:100, 98:102].max(), 0)

    def test_giu_quai_vat_ngang_qua_thanh_va_giu_than_tui(self):
        self.assertTrue((self.out[40:50, 60:140] == 255).all())
        self.assertTrue((self.out[100:180, 40:160] == 255).all())
        self.assertTrue((self.out[50:100, 60:68] == 255).all())

    def test_dao_cu_to_nam_trong_khung_san_pham_khong_bi_dung(self):
        alpha = np.zeros((100, 100), np.uint8)
        alpha[20:80, 20:80] = 255
        alpha[10:20, 30:70] = 255                      # quai mảnh nằm trong khung "đạo cụ" to
        out = svc.isolate_product(alpha, (20, 10, 80, 80), [(25, 5, 75, 60)])
        self.assertTrue((out[10:20, 30:70] == 255).all())


class CutOutProductWithDetectionTests(SimpleTestCase):
    def test_tach_nen_roi_loc_theo_khung_san_pham_truoc_khi_cat_sat_mep(self):
        buffer = io.BytesIO()
        Image.new('RGB', (200, 200), (255, 255, 255)).save(buffer, format='PNG')
        alpha, product, props = _bag_on_stand()

        def remover(image):
            rgba = Image.new('RGBA', image.size, (20, 20, 20, 0))
            rgba.putalpha(Image.fromarray(alpha))
            return rgba

        body = (40, 100, 160, 180)
        png, width, height, body_box = svc.cut_out_product(buffer.getvalue(), remover, lambda image, alpha: (product, props, body))
        self.assertEqual((width, height), (120, 140))   # đúng khung túi 40..160 × 40..180
        self.assertEqual(Image.open(io.BytesIO(png)).size, (120, 140))
        # Thân túi y 100..180 trên ảnh gốc → y 60..140 trên ảnh đã cắt sát mép (bắt đầu từ y 40).
        self.assertEqual(body_box, [0, 60, 120, 140])

    def test_doi_khung_ve_anh_da_cat_va_kep_trong_anh(self):
        self.assertEqual(svc.shift_box((10, 20, 50, 60), 5, 10, 40, 40), [5, 10, 40, 40])
        self.assertIsNone(svc.shift_box((0, 0, 4, 4), 5, 5, 40, 40))
        self.assertIsNone(svc.shift_box(None, 0, 0, 10, 10))


class SolidifyBodyTests(SimpleTestCase):
    def setUp(self):
        # Thân túi kem 100×100: rembg để nửa trong suốt (alpha 120), có một khe thật (alpha 0) và viền mờ.
        self.alpha = np.zeros((200, 200), np.uint8)
        self.alpha[50:150, 50:150] = 120
        self.alpha[90:110, 95:105] = 0          # khe quai thật — nhìn xuyên qua
        self.alpha[50:150, 50] = 40             # viền mềm
        self.out = svc.solidify_body(self.alpha, (50, 50, 150, 150))

    def test_than_nua_trong_suot_duoc_lam_dac(self):
        self.assertEqual(int(self.out[70, 70]), 255)
        self.assertEqual(int(self.out[130, 120]), 255)

    def test_lo_that_va_vien_mem_giu_nguyen(self):
        self.assertEqual(int(self.out[100, 100]), 0)
        self.assertEqual(int(self.out[100, 50]), 40)

    def test_dom_lo_nho_trong_than_duoc_lap(self):
        alpha = self.alpha.copy(); alpha[70:74, 120:124] = 0       # đốm rembg để alpha 0 cạnh dây đai
        self.assertEqual(int(svc.solidify_body(alpha, (50, 50, 150, 150))[72, 122]), 255)

    def test_mep_khung_khong_phai_mep_tui_thi_khong_chua_vien(self):
        # Túi kéo dài xuống dưới khung thân (khung cắt ngang túi) — hàng sát mép khung vẫn được làm đặc.
        alpha = np.zeros((200, 200), np.uint8); alpha[50:190, 50:150] = 120
        self.assertEqual(int(svc.solidify_body(alpha, (50, 50, 150, 150))[149, 100]), 255)

    def test_ngoai_khung_than_khong_dung_toi(self):
        alpha = self.alpha.copy(); alpha[10:40, 10:40] = 120
        self.assertEqual(int(svc.solidify_body(alpha, (50, 50, 150, 150))[20, 20]), 120)

    def test_tui_toi_da_dac_san_khong_doi(self):
        alpha = np.zeros((100, 100), np.uint8); alpha[20:80, 20:80] = 255
        np.testing.assert_array_equal(svc.solidify_body(alpha, (20, 20, 80, 80)), alpha)

    def test_cut_out_product_lam_dac_than_va_lay_lai_mau_that(self):
        # rembg trộn điểm nửa trong suốt với nền đen: thân kem (240,235,220) alpha 120 → màu lưu ~(113,111,104).
        # Làm đặc mà giữ màu đó thì túi kem thành xám xỉn — phải lấy lại màu thật từ ảnh gốc.
        buffer = io.BytesIO()
        Image.new('RGB', (200, 200), (240, 235, 220)).save(buffer, format='PNG')

        def remover(image):
            dark = Image.new('RGBA', image.size, (0, 0, 0, 0))
            return Image.composite(image.convert('RGBA'), dark, Image.fromarray(self.alpha))
        png, width, height, _ = svc.cut_out_product(buffer.getvalue(), remover,
                                                     lambda image, alpha: ((50, 50, 150, 150), [], (50, 50, 150, 150)))
        out = np.asarray(Image.open(io.BytesIO(png)).convert('RGBA'))
        self.assertEqual(out[20, 20].tolist(), [240, 235, 220, 255])   # thân (ảnh đã cắt sát mép từ 50,50)
        self.assertEqual(int(out[50, 50, 3]), 0)                       # khe quai vẫn trong suốt
