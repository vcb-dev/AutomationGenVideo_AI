"""Tạo ảnh sản phẩm — tab "Chị Nhạm cầm SP", chế độ ĐỔI SP 0đ (services/held_swap.py).

Khoá phần thuần logic: đọc biến môi trường + kiểm đủ file model, đặt SP mới theo thân SP cũ và tay cầm (phóng ≤×1,15
giữ đáy, đẩy lên nhưng đáy còn chạm ngón tay đỡ, nhận ra dây đeo dài), gộp mặt nạ quai/dây NỐI LIỀN thân (bỏ phần lấn
sang đồ trên kệ), nhuộm ấm theo nền như FE, và view bắt buộc JWT + báo lỗi đúng mã. Model thật (người cầm SP, SAM,
LaMa) không chạy trong test — chất lượng ảnh đã đánh giá bằng mắt trên 120 ảnh chị Nhạm (bag-detector-training/swap).

Chạy: python manage.py test tests.test_product_image_held_swap
"""
import base64
import io
import os
import tempfile
from unittest import mock

import numpy as np
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.services import held_swap as hs
from video_management.services.product_image_service import ProductImageConfigError
from video_management.views import product_image_views


def _png_b64(size=(8, 8)):
    buffer = io.BytesIO()
    Image.new('RGB', size, (0, 128, 0)).save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode('ascii')


class ReadSwapPathsTests(SimpleTestCase):
    def test_missing_detector_env_names_variable(self):
        with mock.patch.dict(os.environ, {hs.PERSON_DETECTOR_MODEL_ENV: '', hs.SWAP_MODELS_DIR_ENV: '/x'}):
            with self.assertRaisesMessage(ProductImageConfigError, hs.PERSON_DETECTOR_MODEL_ENV):
                hs.read_swap_paths()

    def test_missing_models_dir_env_names_variable(self):
        with tempfile.NamedTemporaryFile(suffix='.onnx') as model:
            with mock.patch.dict(os.environ, {hs.PERSON_DETECTOR_MODEL_ENV: model.name, hs.SWAP_MODELS_DIR_ENV: ''}):
                with self.assertRaisesMessage(ProductImageConfigError, hs.SWAP_MODELS_DIR_ENV):
                    hs.read_swap_paths()

    def test_models_dir_lists_missing_files(self):
        with tempfile.NamedTemporaryFile(suffix='.onnx') as model, tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, hs.SAM_ENCODER_FILE), 'wb').close()
            with mock.patch.dict(os.environ, {hs.PERSON_DETECTOR_MODEL_ENV: model.name, hs.SWAP_MODELS_DIR_ENV: folder}):
                with self.assertRaises(ProductImageConfigError) as ctx:
                    hs.read_swap_paths()
            self.assertIn(hs.LAMA_FILE, str(ctx.exception))
            self.assertNotIn(hs.SAM_ENCODER_FILE + "'", str(ctx.exception).split('thiếu file')[1])

    def test_all_files_present_returns_paths(self):
        with tempfile.NamedTemporaryFile(suffix='.onnx') as model, tempfile.TemporaryDirectory() as folder:
            for f in (hs.SAM_ENCODER_FILE, hs.SAM_DECODER_FILE, hs.SAM_MULTI_DECODER_FILE, hs.LAMA_FILE):
                open(os.path.join(folder, f), 'wb').close()
            with mock.patch.dict(os.environ, {hs.PERSON_DETECTOR_MODEL_ENV: model.name, hs.SWAP_MODELS_DIR_ENV: folder}):
                self.assertEqual(hs.read_swap_paths(), (model.name, folder))


class PlaceProductTests(SimpleTestCase):
    # Ảnh SP 100×200: SP chiếm hàng 0..199, thân ở [0, 100, 100, 200] (quai ở nửa trên), đỉnh quai ở cột 50.
    ROWS, BODY, APEX = (0, 199), (0, 100, 100, 200), 50.0
    OLD_BODY = (400, 500, 600, 600)  # thân cũ 200×100, tâm (500, 550)
    BASE = np.sqrt(200 * 100 / (100 * 100))

    def place(self, grips=(), supports=(), long_strap=False):
        return hs.place_product(self.ROWS, self.APEX, self.BODY, self.OLD_BODY, list(grips), list(supports), long_strap)

    def test_no_hands_matches_old_body_size_and_center(self):
        scale, ox, oy = self.place()
        self.assertAlmostEqual(scale, self.BASE)
        self.assertAlmostEqual(ox + 50 * scale, 500)   # tâm thân mới x = tâm thân cũ
        self.assertAlmostEqual(oy + 150 * scale, 550)  # tâm thân mới y = tâm thân cũ

    def test_grip_puts_handle_apex_in_fist(self):
        # Người dùng soát 13/10: túi đặt theo thân cũ thì quai nổi cao hơn tay — đỉnh quai phải vào giữa nắm tay.
        hand = (510, 380, 550, 440)
        scale, ox, oy = self.place(grips=[hand])
        self.assertAlmostEqual(scale, self.BASE)
        self.assertAlmostEqual(oy, hand[1] + hs.GRIP_IN_HAND * (hand[3] - hand[1]))
        self.assertAlmostEqual(ox + self.APEX * scale, 530)  # đỉnh quai dời ngang vào giữa nắm tay

    def test_short_handle_keeps_body_below_fingers(self):
        # Soát 16/10: túi quai dẹt thấp — đỉnh quai vào khớp ngón thì nắp túi che các ngón. Thân phải nằm dưới
        # GRIP_BODY_CLEAR khung tay; quai vẫn trong nắm tay.
        rows, body = (0, 199), (0, 10, 100, 200)        # quai chỉ cao 10/200
        hand = (510, 380, 550, 440)
        scale, _, oy = hs.place_product(rows, self.APEX, body, self.OLD_BODY, [hand], [], False)
        self.assertAlmostEqual(oy + body[1] * scale, hand[1] + hs.GRIP_BODY_CLEAR * 60)
        self.assertLess(oy, hand[3])                    # đỉnh quai vẫn trong khung tay

    def test_grip_horizontal_shift_is_capped(self):
        hand = (900, 380, 940, 440)  # tay lệch xa thân cũ
        scale, ox, _ = self.place(grips=[hand])
        self.assertAlmostEqual(ox + 50 * scale, 500 + hs.GRIP_SHIFT_MAX * 200)

    def test_grip_and_support_scale_to_fit_between_hands(self):
        grip_hand, support = (480, 380, 520, 440), (440, 760, 560, 800)
        scale, _, oy = self.place(grips=[grip_hand], supports=[support])
        grip = grip_hand[1] + hs.GRIP_IN_HAND * 60
        floor = support[1] + hs.SUPPORT_OVERLAP * 40
        want = (floor - grip) / 199
        self.assertAlmostEqual(scale, float(np.clip(want, self.BASE * hs.SCALE_MIN, self.BASE * hs.SCALE_MAX)))
        self.assertAlmostEqual(oy, grip)

    def test_fit_between_hands_is_clamped_to_real_size_range(self):
        scale, _, _ = self.place(grips=[(480, 0, 520, 20)], supports=[(440, 1900, 560, 1950)])
        self.assertAlmostEqual(scale, self.BASE * hs.SCALE_MAX)
        scale, _, _ = self.place(grips=[(480, 480, 520, 500)], supports=[(440, 520, 560, 540)])
        self.assertAlmostEqual(scale, self.BASE * hs.SCALE_MIN)

    def test_support_only_rests_bottom_on_fingers(self):
        support = (440, 640, 560, 680)
        scale, _, oy = self.place(supports=[support])
        self.assertAlmostEqual(oy + 199 * scale, support[1] + hs.SUPPORT_OVERLAP * 40)

    def test_long_strap_keeps_old_body_placement(self):
        self.assertEqual(self.place(grips=[(480, 0, 520, 20)], long_strap=True), self.place())

    def test_pixels_per_cm_from_grip_hand_then_support_hand(self):
        # Thước đo = bàn tay chị Nhạm: cạnh dài khung tay nắm ≈ GRIP_HAND_CM, tay đỡ ≈ SUPPORT_HAND_CM (đo 15/10).
        self.assertAlmostEqual(hs.pixels_per_cm([(0, 0, 40, 92)], []), 92 / hs.GRIP_HAND_CM)
        self.assertAlmostEqual(hs.pixels_per_cm([], [(0, 0, 163, 30)]), 163 / hs.SUPPORT_HAND_CM)
        self.assertIsNone(hs.pixels_per_cm([], []))

    def test_real_width_sets_body_size_from_hand_ruler_and_keeps_grip(self):
        # Người dùng 15/10: "thêm túi vào, biết kích thước túi, căn khoảng tay chị Nhạm cầm" — thân rộng đúng cm theo tay,
        # không co/giãn theo tay đỡ nữa; đỉnh quai vẫn vào nắm tay.
        grip_hand, support = (510, 380, 550, 440), (440, 760, 560, 800)
        scale, ox, oy = hs.place_product(self.ROWS, self.APEX, self.BODY, self.OLD_BODY, [grip_hand], [support], False, 20)
        per_cm = 60 / hs.GRIP_HAND_CM
        self.assertAlmostEqual(100 * scale, 20 * per_cm)                       # thân SP mới rộng 20cm
        self.assertAlmostEqual(oy, grip_hand[1] + hs.GRIP_IN_HAND * 60)
        self.assertAlmostEqual(ox + self.APEX * scale, 530)
        bigger, _, _ = hs.place_product(self.ROWS, self.APEX, self.BODY, self.OLD_BODY, [grip_hand], [support], False, 30)
        self.assertAlmostEqual(bigger / scale, 1.5)                             # túi 30cm to gấp 1,5 túi 20cm

    def test_real_width_uses_given_ruler_over_hand_ruler(self):
        # Ảnh thư viện truyền thước theo khung ảnh — tay MỞ cầm dây dài làm thước tay sai gần gấp đôi (soát 15/10).
        open_hand = (510, 300, 550, 440)   # khung tay dài cả bàn tay
        scale, _, _ = hs.place_product(self.ROWS, self.APEX, self.BODY, self.OLD_BODY, [open_hand], [], False, 24, 20.0)
        self.assertAlmostEqual(100 * scale, 24 * 20.0)

    def test_real_width_without_hands_falls_back_to_old_body_size(self):
        self.assertEqual(hs.place_product(self.ROWS, self.APEX, self.BODY, self.OLD_BODY, [], [], False, 25), self.place())


class TrimHeldProductTests(SimpleTestCase):
    def test_removes_stand_legs_below_body_and_thin_rod_in_handle(self):
        # Soát 16/10: Kelly trên giá trưng bày — chân giá thòng dưới đáy, cột giá đứng giữa lòng quai.
        rgba = np.zeros((300, 200, 4), np.uint8)
        rgba[100:250, 20:180, 3] = 255                          # thân 20..180 × 100..250
        rgba[20:100, 30:36, 3] = rgba[20:100, 164:170, 3] = 255  # 2 bên quai
        rgba[15:22, 30:170, 3] = 255                            # đỉnh quai
        rgba[30:100, 98:102, 3] = 255                           # cột giá giữa lòng quai
        rgba[250:300, 40:44, 3] = rgba[250:300, 150:154, 3] = 255   # chân giá
        out = hs.trim_to_held_product(rgba, (20, 100, 180, 250))
        self.assertFalse(out[255:, :, 3].any())
        self.assertFalse(out[40:95, 98:102, 3].any())
        self.assertTrue(out[40:95, 30:36, 3].all())             # quai giữ nguyên
        self.assertTrue(out[100:250, 20:180, 3].all())          # thân giữ nguyên


class FrontLayerTests(SimpleTestCase):
    def test_palm_on_new_body_goes_behind_bag_fingers_on_handle_stay_in_front(self):
        # Soát 16/10: lòng bàn tay nắm quai dán đè lên thân túi mới = khoét mảng hình bàn tay vào nắp túi, mất quai.
        front = np.zeros((100, 100), bool); front[20:60, 40:60] = True          # bàn tay nắm quai (khung 40..60 × 20..60)
        scene = mock.Mock(front_mask=front, grips=[(40, 20, 60, 60)])
        alpha = np.zeros((100, 100), np.float32); alpha[30:90, 20:80] = 1       # SP mới: quai 30..45, thân 45..90
        layer = hs.front_layer(scene, alpha, (20, 15, 80, 60), 1.0, 0, 30)      # khung thân trong ảnh SP → 45..90
        self.assertTrue(layer[20:45, 40:60].all())       # ngón tay trên quai vẫn vẽ đè
        self.assertFalse(layer[45:60, 40:60].any())      # phần rơi vào thân túi mới nằm sau túi

    def test_wrist_outside_grip_box_goes_behind_new_handle(self):
        # Soát 16/10: quai túi mới đi ngang cổ tay — dán đè cổ tay thì quai bị cắt cụt.
        front = np.zeros((100, 100), bool); front[20:40, 40:60] = True; front[20:45, 60:75] = True   # ngón + cổ tay
        scene = mock.Mock(front_mask=front, grips=[(40, 20, 60, 40)])
        alpha = np.zeros((100, 100), np.float32); alpha[25:90, 30:80] = 1                          # quai 25..45, thân 45..90
        layer = hs.front_layer(scene, alpha, (0, 20, 50, 65), 1.0, 30, 25)
        self.assertTrue(layer[25:36, 40:60].all())        # ngón tay (tới FINGERTIP_LINE khung tay) vẫn ở trước quai
        self.assertFalse(layer[25:45, 60:75].any())       # cổ tay ra sau quai
        self.assertTrue(layer[20:25, 60:75].all())        # chỗ không có túi giữ nguyên

    def test_no_grip_keeps_front_mask(self):
        front = np.ones((10, 10), bool)
        scene = mock.Mock(front_mask=front, grips=[])
        self.assertIs(hs.front_layer(scene, np.ones((10, 10), np.float32), (0, 0, 10, 10), 1.0, 0, 0), front)


class MatchedGrainTests(SimpleTestCase):
    """Vùng LaMa vẽ lại mịn bệt (512px phóng lên) — người dùng 16/10: "bị mất nét". Phủ hạt cho bằng ảnh thật xung quanh."""

    @staticmethod
    def detail(img, mask):
        from scipy import ndimage
        g = img.astype(np.float32)
        return float((g - ndimage.gaussian_filter(g, (hs.GRAIN_HIGHPASS_SIGMA, hs.GRAIN_HIGHPASS_SIGMA, 0)))[mask].std())

    def setUp(self):
        rng = np.random.default_rng(1)
        self.reference = np.clip(120 + rng.normal(0, 6, (200, 200, 3)), 0, 255).astype(np.uint8)   # ảnh thật có hạt
        self.region = np.zeros((200, 200), bool); self.region[70:130, 70:130] = True
        self.smooth = self.reference.copy(); self.smooth[self.region] = 120                       # vùng vẽ lại mịn bệt

    def test_restores_detail_of_redrawn_area_to_match_surroundings(self):
        out = hs.add_matched_grain(self.smooth, self.region, self.reference)
        ring = ~self.region
        before, after, real = (self.detail(self.smooth, self.region), self.detail(out, self.region),
                               self.detail(self.reference, ring))
        self.assertLess(before, 0.5)
        self.assertAlmostEqual(after, real, delta=real * 0.25)
        np.testing.assert_array_equal(out[~self.region & (np.abs(np.arange(200)[:, None] - 100) > 50)],
                                      self.smooth[~self.region & (np.abs(np.arange(200)[:, None] - 100) > 50)])

    def test_deterministic_and_noop_when_nothing_to_add(self):
        a = hs.add_matched_grain(self.smooth, self.region, self.reference)
        np.testing.assert_array_equal(a, hs.add_matched_grain(self.smooth, self.region, self.reference))
        self.assertIs(hs.add_matched_grain(self.reference, self.region, self.reference), self.reference)  # đủ hạt rồi
        empty = np.zeros_like(self.region)
        self.assertIs(hs.add_matched_grain(self.smooth, empty, self.reference), self.smooth)


class ClassifyHandsTests(SimpleTestCase):
    BODY = (400, 500, 600, 700)  # cao 200

    def test_grip_support_side(self):
        grip, support, side = (480, 440, 520, 500), (450, 680, 560, 720), (380, 580, 420, 620)
        grips, supports, sides, long_strap = hs.classify_hands([grip, support, side], self.BODY)
        self.assertEqual((grips, supports, sides, long_strap), ([grip], [support], [side], False))

    def test_hand_far_above_body_is_long_strap(self):
        _, _, _, long_strap = hs.classify_hands([(480, 100, 520, 140)], self.BODY)
        self.assertTrue(long_strap)

    def test_no_hands(self):
        self.assertEqual(hs.classify_hands([], self.BODY), ([], [], [], False))


class SkinMaskTests(SimpleTestCase):
    def test_skin_tones_in_warm_studio_light_are_skin(self):
        rgb = np.array([[[225, 172, 141], [241, 212, 191], [200, 150, 120]]], np.uint8)
        self.assertTrue(hs.skin_mask(rgb).all())

    def test_dark_shelf_black_bag_navy_shirt_are_not_skin(self):
        rgb = np.array([[[40, 30, 25], [20, 20, 22], [30, 30, 80], [230, 230, 230]]], np.uint8)
        self.assertFalse(hs.skin_mask(rgb).any())


class HandCoreTests(SimpleTestCase):
    def test_picks_skin_blob_touching_box_not_other_skin(self):
        skin = np.zeros((100, 100), bool)
        skin[20:50, 20:50] = True    # bàn tay trong khung
        skin[20:50, 80:95] = True    # da khác (cánh tay người khác / mặt) ngoài khung nới
        core = hs.hand_core(skin, (20, 20, 50, 50))
        self.assertTrue(core[30, 30])
        self.assertFalse(core[30, 90])

    def test_too_little_skin_returns_empty(self):
        skin = np.zeros((100, 100), bool)
        skin[20:22, 20:22] = True
        self.assertFalse(hs.hand_core(skin, (20, 20, 50, 50)).any())


class SegmentHandTests(SimpleTestCase):
    def test_sam_grabbing_background_gap_falls_back_to_skin_core(self):
        # Soát 13/10 ảnh túi hobo: SAM nhắc bằng khung trả khe nền giữa ngón thay vì bàn tay → cả tay bị xoá.
        core = np.zeros((100, 100), bool); core[20:60, 20:60] = True
        gap = np.zeros((100, 100), bool); gap[60:70, 20:60] = True
        sam = mock.Mock(); sam.mask.return_value = gap
        hand = hs.segment_hand(sam, core, (20, 20, 60, 60), [(50, 90)])
        self.assertTrue(hand[40, 40])
        self.assertFalse(hand[65, 40])
        labels = sam.mask.call_args[0][1]
        self.assertEqual(labels[:1], [1])   # điểm dương sâu trong vùng da
        self.assertIn(0, labels)            # điểm âm giữa thân SP cũ

    def test_good_sam_trims_core_to_sam_but_fills_finger_holes(self):
        core = np.zeros((100, 100), bool); core[20:60, 20:60] = True
        core[38:42, 38:42] = False          # vệt sáng giữa ngón lệch luật màu da
        sam = mock.Mock(); sam.mask.return_value = core.copy()
        hand = hs.segment_hand(sam, core, (20, 20, 60, 60), [(50, 90)])
        self.assertTrue(hand[40, 40])       # lỗ đã lấp — SP mới không lộ thành đốm trên ngón

    def test_no_core_uses_sam_box(self):
        sam = mock.Mock(); m = np.zeros((100, 100), bool); m[25:55, 25:55] = True; sam.mask.return_value = m
        hand = hs.segment_hand(sam, np.zeros((100, 100), bool), (20, 20, 60, 60), [(50, 90)])
        self.assertTrue(hand[40, 40])


class HandleByColorTests(SimpleTestCase):
    def _scene(self, background):
        rgb = np.full((80, 80, 3), background, np.uint8)
        rgb[60:80] = (190, 130, 70)                              # thân túi da bò
        rgb[10:60, 38:42] = (190, 130, 70)                       # dây quai cùng màu đi lên tay
        rgb[0:15, 30:50] = (235, 190, 170)                       # bàn tay
        rgb[10:15, 38:42] = (190, 130, 70)                       # đầu dây lộ giữa các ngón
        body = np.zeros((80, 80), bool); body[62:80] = True
        skin = np.zeros((80, 80), bool); skin[0:16, 28:52] = True; skin[10:16, 38:42] = True  # dây lọt vào vùng da
        zone = np.zeros((80, 80), bool); zone[0:62, 20:60] = True
        boxes = np.zeros((80, 80), bool); boxes[0:16, 28:52] = True
        return rgb, body, zone, skin, boxes

    def test_strap_below_and_inside_fist_is_handle_skin_is_not(self):
        rgb, body, zone, skin, boxes = self._scene((40, 30, 25))
        handle = hs.handle_by_color(body, rgb, zone, skin, boxes)
        self.assertTrue(handle[30, 40])       # dây dưới nắm tay
        self.assertTrue(handle[13, 40])       # đầu dây trong nắm tay, nối ra ngoài khung tay
        self.assertFalse(handle[5, 32])       # da tay
        self.assertFalse(handle[30, 25])      # nền

    def test_isolated_bag_colored_spot_inside_hand_is_not_handle(self):
        rgb, body, zone, skin, boxes = self._scene((40, 30, 25))
        rgb[10:60, 38:42] = (40, 30, 25)      # không có dây: chỉ vệt cùng màu túi lọt giữa ngón
        rgb[4:8, 34:37] = (190, 130, 70)
        self.assertFalse(hs.handle_by_color(body, rgb, zone, skin, boxes)[6, 35])

    def test_same_color_background_keeps_only_thin_strap(self):
        # Túi trùng màu nền quanh tay (túi đen trước áo đen) → mảng nền dày không bị coi là quai.
        rgb, body, zone, skin, boxes = self._scene((190, 130, 70))
        handle = hs.handle_by_color(body, rgb, zone, skin, boxes)
        self.assertFalse(handle[35, 25])


class WholeProductMaskTests(SimpleTestCase):
    def test_prompts_body_and_handle_positive_and_grip_negative(self):
        sam = mock.Mock(); sam.mask.return_value = np.ones((4, 4), bool)
        hs.whole_product_mask(sam, (0, 0, 100, 200), (0, 100, 100, 200), [(40, 0, 60, 30)], [[50, 40]])
        points, labels = sam.mask.call_args[0]
        self.assertEqual(points[0], [50, 150])               # giữa thân
        self.assertEqual(points[1], [50, 40])                # quai cũ dưới nắm tay
        self.assertEqual(labels, [1, 1, 0, 2, 3])            # điểm âm ở nắm tay, rồi khung SP


class OldProductMaskTests(SimpleTestCase):
    def test_keeps_connected_strap_and_drops_detached_blob(self):
        body = np.zeros((100, 100), bool); body[50:90, 30:70] = True
        strap = np.zeros_like(body); strap[20:51, 48:52] = True       # quai nối liền thân
        blob = np.zeros_like(body); blob[5:15, 80:95] = True          # đồ trên kệ, tách rời
        hands = np.zeros_like(body)
        mask = hs.old_product_mask(body, [strap | blob], (25, 15, 75, 95), hands)
        self.assertTrue(mask[30, 50])
        self.assertFalse(mask[10, 85])

    def test_hand_pixels_never_part_of_product(self):
        body = np.zeros((60, 60), bool); body[30:50, 10:50] = True
        extra = np.zeros_like(body); extra[20:31, 25:35] = True
        hands = np.zeros_like(body); hands[20:30, 25:35] = True
        mask = hs.old_product_mask(body, [extra], (5, 15, 55, 55), hands)
        self.assertFalse(mask[25, 30])


class HarmonizeTests(SimpleTestCase):
    def test_warm_background_tints_product_warmer(self):
        warm = np.zeros((4, 4, 3), np.uint8); warm[..., 0], warm[..., 1], warm[..., 2] = 200, 120, 60
        w = hs.scene_warmth(warm)
        self.assertGreater(w, 0.5)
        product = np.full((2, 2, 4), 128, np.uint8)
        out = hs.harmonize(product, w)
        self.assertGreater(out[0, 0, 0], out[0, 0, 2])

    def test_neutral_background_leaves_product_unchanged(self):
        grey = np.full((4, 4, 3), 128, np.uint8)
        self.assertEqual(hs.scene_warmth(grey), 0.0)
        product = np.full((2, 2, 4), 100, np.uint8)
        np.testing.assert_allclose(hs.harmonize(product, 0.0), product)


class HeldSwapViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        # Model phóng ảnh SP nhỏ (product_upscale): test view không cần model thật.
        for name, value in (('read_upscale_model_path', '/up.onnx'), ('get_upscaler', mock.Mock(name='upscaler'))):
            patcher = mock.patch.object(product_image_views.up, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.body = {'person_image_base64': _png_b64(), 'person_mime_type': 'image/png',
                     'product_image_base64': _png_b64(), 'product_mime_type': 'image/png'}

    def _call(self, body, authenticate=True):
        request = self.factory.post('/api/ai/product-image/held-swap/', body, format='json')
        if authenticate:
            force_authenticate(request, user=mock.Mock(is_authenticated=True))
        return product_image_views.held_swap(request)

    def test_requires_authentication(self):
        self.assertIn(self._call(self.body, authenticate=False).status_code, (401, 403))

    def test_missing_person_image_is_400(self):
        response = self._call({**self.body, 'person_image_base64': ''})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data['success'])

    def test_missing_swap_config_is_500_naming_variable(self):
        with mock.patch.object(product_image_views.svc, 'read_rembg_model', return_value='isnet-general-use'), \
                mock.patch.object(product_image_views.det, 'read_detector_model_path', return_value='/x.onnx'), \
                mock.patch.object(product_image_views.det, 'get_detector', return_value=mock.Mock(name='d')), \
                mock.patch.dict(os.environ, {hs.PERSON_DETECTOR_MODEL_ENV: ''}):
            response = self._call(self.body)
        self.assertEqual(response.status_code, 500)
        self.assertIn(hs.PERSON_DETECTOR_MODEL_ENV, response.data['error_message'])

    def test_success_returns_jpeg_and_free_usage(self):
        cutout = io.BytesIO(); Image.new('RGBA', (10, 20), (200, 0, 0, 255)).save(cutout, 'PNG')
        detector = mock.Mock(); detector.name = 'bag-detector-x'
        swap_models = {'person': mock.Mock()}; swap_models['person'].name = 'person-detector-y'
        with mock.patch.object(product_image_views.svc, 'read_rembg_model', return_value='isnet-general-use'), \
                mock.patch.object(product_image_views.det, 'read_detector_model_path', return_value='/x.onnx'), \
                mock.patch.object(product_image_views.det, 'get_detector', return_value=detector), \
                mock.patch.object(product_image_views.hs, 'read_swap_paths', return_value=('/p.onnx', '/m')), \
                mock.patch.object(product_image_views.hs, 'get_models', return_value=swap_models), \
                mock.patch.object(product_image_views.svc, 'get_rembg_session', return_value=object()), \
                mock.patch.object(product_image_views.svc, 'cut_out_product', return_value=(cutout.getvalue(), 10, 20, [0, 10, 10, 20])), \
                mock.patch.object(product_image_views.hs, 'swap_product', return_value=(b'jpeg-bytes', {'width': 8, 'height': 8, 'hands': 2, 'warnings': []})) as swap:
            response = self._call(self.body)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['mime_type'], 'image/jpeg')
        self.assertEqual(base64.b64decode(response.data['image_base64']), b'jpeg-bytes')
        self.assertEqual(response.data['usage']['cost_usd'], 0)
        self.assertEqual(response.data['info']['hands'], 2)
        self.assertEqual(swap.call_args[0][3], [0, 10, 10, 20])  # khung thân SP mới từ bước tách nền


class ImageUploadTransportTests(SimpleTestCase):
    """Ảnh máy ảnh của media 8MB+ (13/10): gửi base64 trong JSON thì Django chặn ở DATA_UPLOAD_MAX_MEMORY_SIZE
    (RequestDataTooBig, mặc định 2,5MB) và view từng báo nhầm "Không nạp được model". BE giờ gửi file multipart —
    phần file không tính vào trần đó."""

    BIG = 3 * 1024 * 1024  # vượt trần 2,5MB mặc định của Django

    def setUp(self):
        self.factory = APIRequestFactory()

    def _big_jpeg(self):
        return b'\xff\xd8' + b'0' * self.BIG

    def _swap(self, request):
        force_authenticate(request, user=mock.Mock(is_authenticated=True))
        seen = {}

        def check(data, mime, label):
            seen.setdefault('sizes', []).append(len(data))
            return data, mime
        # Chỉ kiểm bước đọc ảnh: dừng ngay sau đó bằng lỗi cấu hình giả.
        with mock.patch.object(product_image_views.svc, 'check_input_image', side_effect=check), \
                mock.patch.object(product_image_views.svc, 'open_upright_image', return_value=object()), \
                mock.patch.object(product_image_views.svc, 'read_rembg_model',
                                  side_effect=ProductImageConfigError('REMBG_MODEL thiếu')):
            response = product_image_views.held_swap(request)
        return response, seen

    def test_multipart_files_over_django_body_limit_are_read(self):
        request = self.factory.post('/api/ai/product-image/held-swap/', {
            'person_image': SimpleUploadedFile('a.jpg', self._big_jpeg(), content_type='image/jpeg'),
            'person_mime_type': 'image/jpeg',
            'product_image': SimpleUploadedFile('b.png', b'png', content_type='image/png'),
            'product_mime_type': 'image/png',
        }, format='multipart')
        response, seen = self._swap(request)
        # Qua được bước đọc ảnh (tới bước cấu hình), đủ cả 2 ảnh, ảnh lớn không bị cắt.
        self.assertEqual(response.status_code, 500, response.data)
        self.assertIn('REMBG_MODEL', response.data['error_message'])
        self.assertEqual(seen['sizes'], [len(self._big_jpeg()), 3])

    def test_multipart_mime_falls_back_to_file_content_type(self):
        request = self.factory.post('/api/ai/product-image/held-swap/', {
            'person_image': SimpleUploadedFile('a.jpg', b'jpg', content_type='image/jpeg'),
            'product_image': SimpleUploadedFile('b.webp', b'webp', content_type='image/webp'),
        }, format='multipart')
        with mock.patch.object(product_image_views.svc, 'open_upright_image', return_value=object()), \
                mock.patch.object(product_image_views.svc, 'read_rembg_model',
                                  side_effect=ProductImageConfigError('REMBG_MODEL thiếu')):
            force_authenticate(request, user=mock.Mock(is_authenticated=True))
            response = product_image_views.held_swap(request)
        self.assertEqual(response.status_code, 500, response.data)

    def test_oversized_json_base64_is_clear_400_not_model_error(self):
        big = base64.b64encode(self._big_jpeg()).decode()
        request = self.factory.post('/api/ai/product-image/held-swap/', {
            'person_image_base64': big, 'person_mime_type': 'image/jpeg',
            'product_image_base64': _png_b64(), 'product_mime_type': 'image/png',
        }, format='json')
        force_authenticate(request, user=mock.Mock(is_authenticated=True))
        response = product_image_views.held_swap(request)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('multipart', response.data['error_message'])
        self.assertNotIn('nạp được model', response.data['error_message'])

    def test_cutout_reads_multipart_image(self):
        request = self.factory.post('/api/ai/product-image/cutout/', {
            'image': SimpleUploadedFile('a.jpg', self._big_jpeg(), content_type='image/jpeg'),
            'mime_type': 'image/jpeg',
        }, format='multipart')
        force_authenticate(request, user=mock.Mock(is_authenticated=True))
        with mock.patch.object(product_image_views.svc, 'read_rembg_model',
                               side_effect=ProductImageConfigError('REMBG_MODEL thiếu')):
            response = product_image_views.cutout(request)
        # Qua bước đọc ảnh 3MB (kiểm thật, không giả) — dừng ở cấu hình.
        self.assertEqual(response.status_code, 500, response.data)
