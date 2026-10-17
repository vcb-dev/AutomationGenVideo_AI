"""
Tạo ảnh sản phẩm — làm sạch ảnh tách nền (cutout_refine) + phóng ảnh SP nhỏ (product_upscale), 0đ.

Người dùng soát 15/10 thấy ảnh ghép "giả": ảnh lưu từ Google chỉ ~200px (túi bị phóng ~7 lần — vỡ hạt), rembg giữ nhầm
phông trong lòng quai / giữa dây đeo và thân túi, dây mảnh nửa trong suốt thành "bóng ma", mép ám màu phông.
Khoá: xoá đúng mảng phông nằm ngoài thân túi; KHÔNG xoá vệt sáng trên quai bóng, logo/miếng da trên thân, túi cùng màu
phông; bỏ qua ảnh phông lổn nhổn; khử viền ám phông; làm đặc dây mảnh nhưng không làm đặc bóng đổ; đầu ra màu đã nhân
độ đặc như rembg. Phóng ảnh: biến môi trường bắt buộc, ảnh lớn giữ nguyên, ảnh nhỏ phóng ×4, giới hạn cỡ đưa vào model.

Chạy: python manage.py test tests.test_product_image_cutout_refine
"""
import io
import os
import sys
import tempfile
import types
from unittest import mock

import scipy.ndimage  # noqa: F401 — nạp trước mock.patch.dict(sys.modules) (xem test_product_image_detector)
import numpy as np
from django.test import SimpleTestCase
from PIL import Image

from video_management.services import cutout_refine as cr
from video_management.services import product_image_service as svc
from video_management.services import product_upscale as up
from video_management.services.product_image_service import ProductImageConfigError

BACKDROP = (205, 205, 208)
BLACK = (25, 24, 26)


def _scene(size=200, backdrop=BACKDROP):
    rgb = np.zeros((size, size, 3), np.uint8)
    rgb[:] = backdrop
    alpha = np.zeros((size, size), np.uint8)
    return rgb, alpha


def _bag_with_handle(size=200, hole_kept=True, body_color=BLACK, backdrop=BACKDROP):
    """Thân túi chữ nhật + quai vòng phía trên; rembg (giả) giữ nhầm cả lòng quai nếu `hole_kept`."""
    rgb, alpha = _scene(size, backdrop)
    rgb[100:180, 40:160] = body_color
    alpha[100:180, 40:160] = 255
    yy, xx = np.ogrid[:size, :size]
    ring = (np.hypot(yy - 100, xx - 100) <= 50) & (np.hypot(yy - 100, xx - 100) >= 42) & (yy <= 100)
    rgb[ring] = body_color
    alpha[ring] = 255
    hole = (np.hypot(yy - 100, xx - 100) < 42) & (yy < 100)
    if hole_kept:
        alpha[hole] = 255
    return rgb, alpha, hole, ring


class RefineCutoutTests(SimpleTestCase):
    def test_handle_hole_kept_by_rembg_is_removed_but_handle_and_body_stay(self):
        rgb, alpha, hole, ring = _bag_with_handle()
        rgba, info = cr.refine_cutout(rgb, alpha, alpha)
        self.assertTrue(info['applied'], info)
        self.assertGreater(info['leftover_removed'], 0.05)
        self.assertEqual(rgba[70, 100, 3], 0)                         # giữa lòng quai: phông → trong suốt
        inner_ring = ring & ~scipy.ndimage.binary_dilation(~ring, iterations=2)
        self.assertTrue((rgba[..., 3][inner_ring] == 255).all())       # quai còn nguyên
        self.assertEqual(rgba[140, 100, 3], 255)                       # thân còn nguyên

    def test_thick_handle_loop_is_still_cleared_using_the_body_box(self):
        # Quai dày ~7% bề ngang túi (ảnh phóng ×2): gọt theo khung thân để lòng quai không bị coi là "trên thân".
        rgb, alpha, hole, ring = _bag_with_handle()
        big = lambda a, mode: np.asarray(Image.fromarray(a).resize((400, 400), mode))
        rgba, info = cr.refine_cutout(big(rgb, Image.BICUBIC), big(alpha, Image.NEAREST), big(alpha, Image.NEAREST),
                                      body_box=[80, 200, 320, 360])
        self.assertGreater(info['leftover_removed'], 0.05, info)
        self.assertEqual(rgba[140, 200, 3], 0)

    def test_thin_glossy_highlight_on_handle_is_kept(self):
        # Quai da bóng phản chiếu phông xám: vệt sáng mảnh nằm giữa quai (soát 15/10 — từng bị xoá thành quai rách).
        rgb, alpha, hole, ring = _bag_with_handle(hole_kept=False)
        yy, xx = np.ogrid[:200, :200]
        streak = (np.abs(np.hypot(yy - 100, xx - 100) - 46) <= 1) & (yy <= 95)
        rgb[streak] = BACKDROP
        rgba, info = cr.refine_cutout(rgb, alpha, alpha)
        self.assertTrue((rgba[..., 3][streak] == 255).all(), info)

    def test_large_glossy_highlight_on_a_handle_tab_is_kept(self):
        # Miếng da đính quai bóng phản chiếu phông: vệt sáng tròn lớn nằm giữa miếng da, ngoài thân túi (soát 15/10).
        rgb, alpha, hole, ring = _bag_with_handle(hole_kept=False)
        rgb[50:110, 0:60] = BLACK
        alpha[50:110, 0:60] = 255                                     # miếng da đính quai chìa ra ngoài thân
        rgb[70:90, 20:40] = BACKDROP                                  # vệt sáng giữa miếng da, da bao quanh dày 20px
        rgba, info = cr.refine_cutout(rgb, alpha, alpha, body_box=[40, 100, 160, 180])
        self.assertTrue((rgba[72:88, 22:38, 3] == 255).all(), info)

    def test_grey_reflection_inside_the_body_box_is_kept(self):
        # Da bóng phản chiếu xám ở mép trên thân, nối ra phông qua lòng quai (soát 15/10 — từng bị xoá thành mảng khuyết).
        rgb, alpha, hole, ring = _bag_with_handle(hole_kept=False)
        rgb[100:125, 70:110] = (np.array(BACKDROP) * 0.8).astype(np.uint8)
        rgba, info = cr.refine_cutout(rgb, alpha, alpha, body_box=[40, 100, 160, 180])
        self.assertTrue((rgba[102:123, 72:108, 3] == 255).all(), info)

    def test_backdrop_coloured_patch_on_body_is_kept(self):
        rgb, alpha, hole, ring = _bag_with_handle(hole_kept=False)
        rgb[125:155, 80:120] = BACKDROP                                # miếng da/nhãn trắng giữa thân túi
        rgba, info = cr.refine_cutout(rgb, alpha, alpha)
        self.assertEqual(info['leftover_removed'], 0.0)
        self.assertTrue((rgba[128:152, 83:117, 3] == 255).all())

    def test_bag_same_colour_as_backdrop_is_left_alone(self):
        rgb, alpha, hole, ring = _bag_with_handle(body_color=(210, 210, 212))
        rgba, info = cr.refine_cutout(rgb, alpha, alpha)
        self.assertEqual(info['reason'], 'SP gần cùng màu phông')
        self.assertTrue((rgba[..., 3][hole] == 255).all())             # túi trắng trên phông trắng: không xoá gì

    def test_busy_backdrop_is_skipped_untouched(self):
        rgb, alpha, hole, ring = _bag_with_handle()
        noise = np.random.default_rng(0).integers(0, 255, rgb.shape, dtype=np.uint8)
        rgb[alpha == 0] = noise[alpha == 0]                            # ảnh chụp ngoài phố / kệ shop
        rgba, info = cr.refine_cutout(rgb, alpha, alpha)
        self.assertFalse(info['applied'])
        self.assertIn('phông không trơn', info['reason'])
        np.testing.assert_array_equal(rgba[..., 3], alpha)

    def test_light_fringe_around_dark_bag_is_decontaminated(self):
        rgb, alpha = _scene()
        rgb[60:140, 60:140] = BLACK
        alpha[60:140, 60:140] = 255
        rgb[60:140, 59] = rgb[60:140, 140] = (128, 128, 130)           # điểm mép trộn nửa đen nửa phông, rembg cho đặc
        alpha[60:140, 59] = alpha[60:140, 140] = 255
        rgba, info = cr.refine_cutout(rgb, alpha, alpha)
        a = rgba[100, 59, 3] / 255
        self.assertLess(a, 0.75)                                       # độ đặc theo màu ≈ 0,5
        self.assertGreater(a, 0.25)
        straight = rgba[100, 59, :3] / max(a, 1e-6)
        self.assertLess(straight.max(), 60)                            # màu thật của mép là đen, không còn xám sáng

    def test_thin_strap_left_half_transparent_by_rembg_becomes_solid(self):
        rgb, alpha = _scene()
        rgb[20:180, 99:101] = (200, 120, 110)                          # dây đeo hồng mảnh, không có điểm đặc gần đó
        alpha[20:180, 99:101] = 100
        rgba, info = cr.refine_cutout(rgb, alpha, alpha)
        self.assertTrue((rgba[30:170, 99:101, 3] == 255).all(), info)

    def test_soft_shadow_on_backdrop_is_not_made_solid(self):
        rgb, alpha = _scene()
        rgb[60:120, 60:140] = BLACK
        alpha[60:120, 60:140] = 255
        shadow = (np.array(BACKDROP) * 0.6).astype(np.uint8)           # bóng đổ dưới đáy túi trên phông
        rgb[120:130, 60:140] = shadow
        alpha[120:130, 60:140] = 70
        rgba, info = cr.refine_cutout(rgb, alpha, alpha)
        self.assertTrue((rgba[121:129, 70:130, 3] <= 70).all(), info)

    def test_source_shadow_left_faint_far_below_the_bag_is_removed(self):
        # Bóng của ảnh gốc trên phông, cách đáy túi 20–45px, rembg giữ mờ (soát 15/10: thành "kính mờ" trên hộp gỗ).
        rgb, alpha = _scene()
        rgb[40:120, 60:140] = BLACK
        alpha[40:120, 60:140] = 255
        rgb[140:165, 50:150] = (np.array(BACKDROP) * 0.8).astype(np.uint8)
        alpha[140:165, 50:150] = 90
        rgb[170:180, 50:150] = BACKDROP
        alpha[170:180, 50:150] = 60
        rgba, info = cr.refine_cutout(rgb, alpha, alpha)
        self.assertTrue((rgba[140:180, 50:150, 3] == 0).all(), info)
        self.assertTrue((rgba[45:115, 65:135, 3] == 255).all())

    def test_shadowed_floor_enclosed_by_a_strap_lying_on_it_is_removed(self):
        # Ảnh túi đặt trên sàn studio, dây đeo nằm vòng trên sàn: khoảng sàn giữa dây và đáy túi tối hơn phông ~8% (soát
        # 15/10 ảnh Pexels — thành mảng trắng dưới đáy túi khi ghép lên hộp gỗ).
        rgb, alpha = _scene()
        rgb[40:110, 60:140] = (150, 70, 40)
        alpha[40:110, 60:140] = 255
        floor = (np.array(BACKDROP) * 0.92).astype(np.uint8)
        rgb[110:150, 40:160] = floor
        alpha[110:150, 40:160] = 255                                   # rembg giữ cả khoảng sàn
        rgb[110:116, 60:140] = (125, 122, 145)                          # bóng tiếp xúc tối, ngả tím, rembg không chắc
        alpha[110:116, 60:140] = 230
        for rows, cols in ((np.s_[150:156], np.s_[34:166]), (np.s_[100:156], np.s_[34:40]), (np.s_[100:156], np.s_[160:166])):
            rgb[rows, cols] = (120, 60, 35)
            alpha[rows, cols] = 255                                    # dây đeo nằm quanh khoảng sàn
        rembg = alpha.copy()
        alpha[110:116, 60:140] = 255                                   # solidify_body đã làm đặc dải bóng (khung thân dài hơn)
        rgba, info = cr.refine_cutout(rgb, alpha, rembg, body_box=[60, 40, 140, 116])
        # Ảnh thử chỉ 200px nên vài điểm sàn sát đáy túi quá xa mọi điểm phông để đoán màu — ảnh thật lớn hơn nhiều.
        self.assertGreater(float((rgba[115:146, 45:155, 3] == 0).mean()), 0.95, info)
        self.assertTrue((rgba[45:105, 65:135, 3] == 255).all())
        self.assertGreater(float((rgba[111:115, 65:135, 3] == 0).mean()), 0.8)  # bóng tiếp xúc sát đáy cũng xoá
        self.assertTrue((rgba[151:155, 50:150, 3] == 255).all())       # dây vẫn giữ

    def test_output_colours_are_premultiplied_like_rembg(self):
        rgb, alpha = _scene()
        rgb[50:150, 50:150] = (200, 60, 60)
        alpha[50:150, 50:150] = 255
        alpha[50:150, 150] = 128
        rgb[50:150, 150] = (200, 60, 60)
        rgba, _ = cr.refine_cutout(rgb, alpha, alpha)
        a = rgba[..., 3].astype(int)
        partial = (a > 0) & (a < 255)
        self.assertTrue(partial.any())
        self.assertTrue((rgba[..., 0][partial].astype(int) <= a[partial] + 1).all())
        self.assertTrue((rgba[a == 0] == 0).all())

    def test_product_filling_the_frame_is_left_alone(self):
        rgb = np.full((50, 50, 3), 30, np.uint8)
        alpha = np.full((50, 50), 255, np.uint8)
        rgba, info = cr.refine_cutout(rgb, alpha, alpha)
        self.assertEqual(info['reason'], 'ảnh không lộ phông')
        np.testing.assert_array_equal(rgba[..., 3], alpha)


class UpscaleConfigTests(SimpleTestCase):
    def setUp(self):
        up._sessions.clear()
        self.addCleanup(up._sessions.clear)

    def test_missing_variable_names_it(self):
        with mock.patch.dict(os.environ, {'PRODUCT_IMAGE_UPSCALE_MODEL_PATH': ''}):
            with self.assertRaisesMessage(ProductImageConfigError, 'PRODUCT_IMAGE_UPSCALE_MODEL_PATH'):
                up.read_upscale_model_path()

    def test_missing_file_names_path(self):
        with mock.patch.dict(os.environ, {'PRODUCT_IMAGE_UPSCALE_MODEL_PATH': '/khong/co/sr.onnx'}):
            with self.assertRaisesMessage(ProductImageConfigError, '/khong/co/sr.onnx'):
                up.read_upscale_model_path()

    def test_invalid_model_names_variable_and_session_is_cached(self):
        fake_ort = types.ModuleType('onnxruntime')
        fake_ort.SessionOptions = mock.Mock
        fake_ort.InferenceSession = mock.Mock(side_effect=RuntimeError('INVALID_PROTOBUF'))
        with mock.patch.dict(sys.modules, {'onnxruntime': fake_ort}):
            with self.assertRaisesMessage(ProductImageConfigError, 'PRODUCT_IMAGE_UPSCALE_MODEL_PATH'):
                up.get_upscaler('/models/hong.onnx')
            fake_ort.InferenceSession = mock.Mock(return_value='session')
            self.assertEqual(up.get_upscaler('/models/sr.onnx'), 'session')
            self.assertEqual(up.get_upscaler('/models/sr.onnx'), 'session')
            fake_ort.InferenceSession.assert_called_once()


class UpscaleIfSmallTests(SimpleTestCase):
    @staticmethod
    def _session():
        session = mock.Mock()
        session.get_inputs.return_value = [mock.Mock()]
        session.get_inputs.return_value[0].name = 'input'
        # Model giả ×4: lặp mỗi điểm 4×4 lần.
        session.run.side_effect = lambda _, feed: [feed['input'].repeat(4, axis=2).repeat(4, axis=3)]
        return session

    def test_large_image_is_untouched(self):
        session = self._session()
        image = Image.new('RGB', (up.UPSCALE_BELOW_SIDE_PX, 300))
        out, factor = up.upscale_if_small(session, image)
        self.assertIs(out, image)
        self.assertEqual(factor, 1)
        session.run.assert_not_called()

    def test_small_image_is_upscaled_four_times_from_rgb_0_1(self):
        session = self._session()
        image = Image.new('RGB', (192, 256), (255, 0, 0))
        out, factor = up.upscale_if_small(session, image)
        self.assertEqual((out.size, factor), ((768, 1024), 4))
        feed = session.run.call_args[0][1]['input']
        self.assertEqual(feed.shape, (1, 3, 256, 192))
        self.assertAlmostEqual(float(feed[0, 0].max()), 1.0)
        self.assertEqual(out.getpixel((10, 10)), (255, 0, 0))

    def test_input_is_capped_before_the_model(self):
        session = self._session()
        out, factor = up.upscale_if_small(session, Image.new('RGB', (680, 340)))
        side = up.UPSCALE_MAX_INPUT_SIDE_PX
        self.assertEqual(session.run.call_args[0][1]['input'].shape[2:], (side // 2, side))
        self.assertEqual(out.size, (side * 4, side * 2))


class CutOutProductPipelineTests(SimpleTestCase):
    def test_upscale_runs_before_rembg_and_info_is_reported(self):
        buffer = io.BytesIO()
        rgb, alpha, hole, ring = _bag_with_handle()
        Image.fromarray(rgb).save(buffer, 'PNG')
        seen = {}

        def upscale(image):
            seen['upscaled_from'] = image.size
            return image.resize((image.width * 2, image.height * 2)), 2

        def remove(image):
            seen['rembg_size'] = image.size
            out = np.dstack([np.asarray(image), np.asarray(Image.fromarray(alpha).resize(image.size, Image.NEAREST))])
            return Image.fromarray(out, 'RGBA')

        info = {}
        locate = lambda image, a: ((0, 0, image.width, image.height), [], (80, 200, 320, 360))
        png, width, height, body = svc.cut_out_product(buffer.getvalue(), remove, locate, upscale=upscale, info=info)
        self.assertEqual(seen, {'upscaled_from': (200, 200), 'rembg_size': (400, 400)})
        self.assertEqual(info['source_size'], [200, 200])
        self.assertEqual(info['upscaled'], 2)
        self.assertTrue(info['refine']['applied'])
        self.assertGreater(info['refine']['leftover_removed'], 0)    # lòng quai giữ nhầm đã được xoá
        cutout = np.asarray(Image.open(io.BytesIO(png)))
        self.assertEqual(cutout[..., 3].max(), 255)
