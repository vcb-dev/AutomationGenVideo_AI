"""
Tạo ảnh sản phẩm — tab "Chị Nhạm cầm SP", chế độ TỰ CHỌN TƯ THẾ (0đ): người dùng chỉ thêm ảnh SP mới, AI chấm điểm các
tư thế chị Nhạm dựng sẵn (services/pose_library.py) rồi ghép vào các tư thế hợp nhất.

Khoá: biến môi trường bắt buộc (báo đúng tên biến), lọc tư thế dùng được, lưu/nạp thư viện giữ nguyên mặt nạ + khung,
chấm điểm ưu tiên tư thế mà túi mới che kín chỗ SP cũ đã xoá và khớp quai, ghép đúng số ảnh theo thứ tự điểm, endpoint
held-auto (xác thực, lỗi đầu vào/cấu hình, phản hồi thành công 0đ).
"""
import base64
import io
import json
import os
import tempfile
from unittest import mock

import numpy as np
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.services import held_swap as hs
from video_management.services import pose_library as pl
from video_management.services.product_image_service import ProductImageConfigError
from video_management.views import product_image_views


def make_scene(h=200, w=120, body=(30, 100, 90, 180), bag=(30, 60, 90, 180), grips=((50, 40, 70, 70),),
               supports=((30, 175, 90, 190),), long_strap=False):
    rgb = np.full((h, w, 3), 90, np.uint8)
    old = np.zeros((h, w), bool); old[int(bag[1]):int(bag[3]), int(bag[0]):int(bag[2])] = True
    hand = np.zeros((h, w), bool)
    for hb in list(grips) + list(supports):
        hand[int(hb[1]):int(hb[3]), int(hb[0]):int(hb[2])] = True
    front = hand.copy()
    for hb in supports:
        front[int(hb[1]):int(hb[3]), int(hb[0]):int(hb[2])] = False
    hands = list(grips) + list(supports)
    return hs.Scene(rgb, rgb.copy(), None, old & ~hand, hand, front, bag, body, hands, list(grips), list(supports),
                    long_strap, detector='person-x')


class LibrarySettingsTests(SimpleTestCase):
    def test_missing_dir_names_variable(self):
        with mock.patch.dict(os.environ, {pl.POSE_LIBRARY_DIR_ENV: '', pl.POSE_CANDIDATES_ENV: '3'}):
            with self.assertRaisesMessage(ProductImageConfigError, pl.POSE_LIBRARY_DIR_ENV):
                pl.read_library_settings()

    def test_dir_without_index_is_config_error(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {pl.POSE_LIBRARY_DIR_ENV: folder, pl.POSE_CANDIDATES_ENV: '3'}):
            with self.assertRaisesMessage(ProductImageConfigError, pl.INDEX_FILE):
                pl.read_library_settings()

    def test_count_required_and_positive(self):
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, pl.INDEX_FILE), 'w').write('{"poses": []}')
            for raw in ('', '0', 'ba'):
                with mock.patch.dict(os.environ, {pl.POSE_LIBRARY_DIR_ENV: folder, pl.POSE_CANDIDATES_ENV: raw}):
                    with self.assertRaisesMessage(ProductImageConfigError, pl.POSE_CANDIDATES_ENV):
                        pl.read_library_settings()
            with mock.patch.dict(os.environ, {pl.POSE_LIBRARY_DIR_ENV: folder, pl.POSE_CANDIDATES_ENV: '3'}):
                self.assertEqual(pl.read_library_settings(), (folder, 3))


class PoseUsableTests(SimpleTestCase):
    def test_reasons(self):
        self.assertEqual(pl.pose_usable(make_scene()), (True, ''))
        self.assertFalse(pl.pose_usable(make_scene(long_strap=True))[0])
        self.assertFalse(pl.pose_usable(make_scene(grips=(), supports=()))[0])


class SaveLoadTests(SimpleTestCase):
    def test_roundtrip_keeps_masks_boxes_and_images(self):
        scene = make_scene()
        with tempfile.TemporaryDirectory() as folder:
            meta = pl.save_pose(scene, folder, 'pose1', 'DSC1.JPG')
            pl.write_index(folder, [meta])
            poses = pl.load_library(folder)
            self.assertEqual([p.id for p in poses], ['pose1'])
            loaded = poses[0].scene()
        np.testing.assert_array_equal(loaded.hand_mask, scene.hand_mask)
        np.testing.assert_array_equal(loaded.front_mask, scene.front_mask)
        np.testing.assert_array_equal(loaded.old_mask, scene.old_mask)
        self.assertEqual(loaded.rgb.shape, scene.rgb.shape)
        self.assertEqual(loaded.body, tuple(float(v) for v in scene.body))
        self.assertEqual(loaded.grips, [tuple(float(v) for v in hb) for hb in scene.grips])
        self.assertFalse(loaded.long_strap)
        self.assertEqual(meta['source'], 'DSC1.JPG')

    def test_excluded_poses_are_skipped(self):
        # Tư thế soát bằng mắt thấy còn sót dây/mảnh túi cũ ghi vào excluded.json — không bao giờ được chọn để ghép.
        with tempfile.TemporaryDirectory() as folder:
            metas = [pl.save_pose(make_scene(), folder, pid, f'{pid}.JPG') for pid in ('keep', 'drop')]
            pl.write_index(folder, metas)
            with open(os.path.join(folder, pl.EXCLUDED_FILE), 'w', encoding='utf-8') as f:
                json.dump({'drop': 'còn dây đeo trắng của túi cũ'}, f, ensure_ascii=False)
            self.assertEqual([p.id for p in pl.load_library(folder)], ['keep'])

    def test_poses_with_broken_grip_hand_mask_are_skipped(self):
        # Mặt nạ tay nắm phủ < MIN_GRIP_COVERAGE phần màu da trong khung tay = tách tay hỏng → mảng thịt dị dạng.
        with tempfile.TemporaryDirectory() as folder:
            metas = [pl.save_pose(make_scene(), folder, pid, f'{pid}.JPG') for pid in ('good', 'broken')]
            metas[0]['grip_coverage'], metas[1]['grip_coverage'] = 0.9, pl.MIN_GRIP_COVERAGE - 0.1
            for meta in metas:
                with open(os.path.join(folder, meta['id'], pl.META_FILE), 'w', encoding='utf-8') as f:
                    json.dump(meta, f)
            pl.write_index(folder, metas)
            self.assertEqual([p.id for p in pl.load_library(folder)], ['good'])

    def test_grip_coverage_measures_skin_covered_by_front_mask(self):
        rgb = np.zeros((20, 20, 3), np.uint8); rgb[:, :] = (200, 150, 120)       # màu da
        front = np.zeros((20, 20), bool); front[:, :10] = True
        self.assertAlmostEqual(pl.grip_coverage(rgb, front, [(0, 0, 20, 20)]), 0.5)
        self.assertIsNone(pl.grip_coverage(rgb, front, []))

    def test_empty_library_is_config_error(self):
        with tempfile.TemporaryDirectory() as folder:
            pl.write_index(folder, [])
            with self.assertRaises(ProductImageConfigError):
                pl.load_library(folder)


class ScoreTests(SimpleTestCase):
    def _pose(self, scene):
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(folder))
        pl.save_pose(scene, folder, 'p', 's')
        with open(os.path.join(folder, 'p', pl.META_FILE)) as f:
            return pl.Pose(folder, json.load(f))

    @staticmethod
    def product(w, h, handle_rows=0):
        """SP chữ nhật w×h; `handle_rows` hàng trên cùng là quai mảnh giữa ảnh."""
        rgba = np.zeros((h, w, 4), np.uint8)
        rgba[handle_rows:, :, 3] = 255
        if handle_rows:
            rgba[:handle_rows, w // 2 - 2:w // 2 + 2, 3] = 255
        return rgba, [0, handle_rows, w, h]

    def test_product_matching_old_shape_beats_tiny_or_strapless_one(self):
        pose = self._pose(make_scene())
        good, good_body = self.product(60, 120, handle_rows=40)   # quai 40/80 = 0,5 thân — như SP cũ
        flat, flat_body = self.product(60, 40)                     # ví không quai
        good_score, detail = pl.score_pose(pose, good[..., 3].copy(), *hs.product_geometry(good), good_body)
        flat_score, _ = pl.score_pose(pose, flat[..., 3].copy(), *hs.product_geometry(flat), flat_body)
        self.assertLess(good_score, flat_score)
        self.assertLess(detail['handle_mismatch'], 0.2)

    def test_known_size_uses_frame_ruler_and_scores_size_gap(self):
        # Thước cm của ảnh thư viện = khung ảnh (FRAME_HEIGHT_CM): ảnh cao 200px → 200/96 px/cm; thân cũ rộng 60px.
        pose = self._pose(make_scene())
        rgba, body = self.product(60, 120, handle_rows=40)
        old_cm = 60 / (200 / pl.FRAME_HEIGHT_CM)
        self.assertAlmostEqual(pl.old_bag_cm(pose.meta), old_cm)
        self.assertAlmostEqual(pl.size_mismatch(pose.meta, old_cm), 0.0, places=6)
        self.assertAlmostEqual(pl.size_mismatch(pose.meta, old_cm * 2), np.log(2), places=6)
        self.assertEqual(pl.size_mismatch(pose.meta, None), 0.0)
        same, d_same = pl.score_pose(pose, rgba[..., 3].copy(), *hs.product_geometry(rgba), body, old_cm)
        double, d_double = pl.score_pose(pose, rgba[..., 3].copy(), *hs.product_geometry(rgba), body, old_cm * 2)
        self.assertLess(d_same['size_mismatch'], d_double['size_mismatch'])
        self.assertLess(same, double)

    def test_pose_holding_other_size_ranks_after_every_same_size_pose(self):
        # Soát 15/10: túi 24cm đặt vào tư thế nâng ví 11cm sát mặt trông "to hơn cả người" — tư thế cầm ví có điểm che kín
        # túi cũ rất tốt nên trọng số thôi không chặn được; lệch cỡ quá SIZE_GATE phải xếp sau mọi tư thế cùng cỡ.
        wallet = self._pose(make_scene(body=(50, 150, 70, 180), bag=(50, 150, 70, 180)))   # thân cũ 20px ≈ 9,6cm
        bag = self._pose(make_scene())                                                     # thân cũ 60px ≈ 28,8cm
        rgba, body = self.product(60, 120, handle_rows=40)
        ranked = pl.rank_poses([wallet, bag], rgba, body, 28.0)
        self.assertEqual([item[1] for item in ranked], [bag, wallet])
        self.assertGreater(ranked[1][2]['size_mismatch'], pl.SIZE_GATE)

    def test_shape_iou_measures_how_well_new_bag_matches_old_bag(self):
        pose = self._pose(make_scene())
        same, same_body = self.product(60, 120, handle_rows=40)   # cùng dáng túi cũ (thân 60×80 + quai)
        flat, flat_body = self.product(60, 40)                     # ví dẹt không quai
        _, d_same = pl.score_pose(pose, same[..., 3].copy(), *hs.product_geometry(same), same_body)
        _, d_flat = pl.score_pose(pose, flat[..., 3].copy(), *hs.product_geometry(flat), flat_body)
        self.assertGreater(d_same['shape_iou'], d_flat['shape_iou'])
        self.assertGreaterEqual(d_same['shape_iou'], pl.MIN_SHAPE_IOU)

    def test_usable_fit_gates_coverage_shape_and_size(self):
        ok = {'uncovered': 0.1, 'outside': 0.0, 'shape_iou': 0.8, 'size_mismatch': 0.05}
        self.assertTrue(pl.usable_fit(ok))
        self.assertFalse(pl.usable_fit({**ok, 'uncovered': pl.MAX_UNCOVERED + 0.01}))   # lộ vết xoá túi cũ
        self.assertFalse(pl.usable_fit({**ok, 'outside': pl.MAX_UNCOVERED + 0.01}))     # tràn khỏi ảnh
        self.assertFalse(pl.usable_fit({**ok, 'shape_iou': pl.MIN_SHAPE_IOU - 0.01}))   # khác dáng → tay nắm vào khoảng không
        self.assertFalse(pl.usable_fit({**ok, 'size_mismatch': pl.MAX_SIZE_MISMATCH + 0.01}))  # túi 24cm vào tư thế cầm ví

    def test_rank_orders_by_score(self):
        poses = [self._pose(make_scene()), self._pose(make_scene(bag=(30, 98, 90, 180)))]
        rgba, body = self.product(60, 120, handle_rows=40)
        ranked = pl.rank_poses(poses, rgba, body)
        self.assertEqual(len(ranked), 2)
        self.assertLessEqual(ranked[0][0], ranked[1][0])


class PickDistinctTests(SimpleTestCase):
    @staticmethod
    def pose(pid, body):
        return mock.Mock(id=pid, meta={'width': 100, 'height': 100, 'body': body})

    def test_skips_near_duplicate_pose_then_backfills(self):
        a = (0.1, self.pose('a', [10, 10, 50, 50]), {})
        a_twin = (0.2, self.pose('a2', [11, 11, 51, 51]), {})   # ảnh chụp liên tiếp cùng tư thế
        b = (0.3, self.pose('b', [50, 50, 90, 90]), {})
        self.assertEqual([i[1].id for i in pl.pick_distinct([a, a_twin, b], 2)], ['a', 'b'])
        self.assertEqual([i[1].id for i in pl.pick_distinct([a, a_twin, b], 3)], ['a', 'b', 'a2'])


class ComposeBestTests(SimpleTestCase):
    def test_composes_top_count_in_rank_order(self):
        poses = [mock.Mock(id=f'p{i}', meta={'source': f's{i}', 'width': 100, 'height': 100, 'body': [i * 25, 0, i * 25 + 20, 20]})
                 for i in range(4)]
        fit = {'uncovered': 0.1, 'outside': 0.0, 'shape_iou': 0.8, 'size_mismatch': 0.0}
        ranked = [(0.1 * i, poses[i], fit) for i in (2, 0, 3, 1)]
        with mock.patch.object(pl, 'rank_poses', return_value=ranked), \
                mock.patch.object(pl.hs, 'compose_product', side_effect=lambda *a, **k: (b'jpg', {'hands': 2})) as compose:
            results = pl.compose_best({}, poses, np.zeros((2, 2, 4), np.uint8), [0, 0, 2, 2], 2, 24.0)
        self.assertEqual([info['pose_id'] for _, info in results], ['p2', 'p0'])
        self.assertEqual(compose.call_count, 2)
        self.assertEqual(compose.call_args.kwargs['width_cm'], 24.0)   # cỡ thật người dùng nhập đi tới bước ghép
        self.assertAlmostEqual(compose.call_args.kwargs['px_per_cm'], 100 / pl.FRAME_HEIGHT_CM)  # thước theo khung ảnh
        self.assertEqual(results[0][1]['pose_source'], 's2')


class ComposeBestGateTests(SimpleTestCase):
    def test_no_pose_fits_raises_clear_input_error(self):
        # Túi dây dài thân thấp (soát 15/10): không tư thế nào cùng dáng → báo rõ, không ghép ra ảnh lệch.
        poses = [mock.Mock(id='p0', meta={'source': 's', 'width': 100, 'height': 100, 'body': [0, 0, 20, 20]})]
        bad = {'uncovered': 0.7, 'outside': 0.0, 'shape_iou': 0.3, 'size_mismatch': 0.1}
        with mock.patch.object(pl, 'rank_poses', return_value=[(0.5, poses[0], bad)]), \
                mock.patch.object(pl.hs, 'compose_product') as compose:
            with self.assertRaisesMessage(pl.ProductImageInputError, 'cùng dáng'):
                pl.compose_best({}, poses, np.zeros((2, 2, 4), np.uint8), [0, 0, 2, 2], 3, 24.0)
        compose.assert_not_called()


class HeldAutoViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        # Model phóng ảnh SP nhỏ (product_upscale): test view không cần model thật.
        for name, value in (('read_upscale_model_path', '/up.onnx'), ('get_upscaler', mock.Mock(name='upscaler'))):
            patcher = mock.patch.object(product_image_views.up, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self, files=None, authenticate=True):  # noqa: D401
        request = self.factory.post('/api/ai/product-image/held-auto/', files or {}, format='multipart')
        if authenticate:
            force_authenticate(request, user=mock.Mock(is_authenticated=True))
        return product_image_views.held_auto(request)

    @staticmethod
    def _image():
        buf = io.BytesIO(); Image.new('RGB', (8, 8), (200, 0, 0)).save(buf, 'PNG')
        return {'product_image': SimpleUploadedFile('sp.png', buf.getvalue(), content_type='image/png'),
                'product_mime_type': 'image/png'}

    def test_requires_authentication(self):
        self.assertIn(self._call(self._image(), authenticate=False).status_code, (401, 403))

    def test_missing_image_is_400(self):
        self.assertEqual(self._call({}).status_code, 400)

    def test_missing_library_env_is_500_naming_variable(self):
        with mock.patch.dict(os.environ, {pl.POSE_LIBRARY_DIR_ENV: ''}):
            response = self._call(self._image())
        self.assertEqual(response.status_code, 500)
        self.assertIn(pl.POSE_LIBRARY_DIR_ENV, response.data['error_message'])

    def test_success_returns_candidates_and_free_usage(self):
        cutout = io.BytesIO(); Image.new('RGBA', (10, 20), (200, 0, 0, 255)).save(cutout, 'PNG')
        detector = mock.Mock(); detector.name = 'bag-detector-x'
        swap_models = {'person': mock.Mock()}; swap_models['person'].name = 'person-detector-y'
        results = [(b'jpeg-a', {'pose_id': 'p1', 'score': 0.1}), (b'jpeg-b', {'pose_id': 'p2', 'score': 0.2})]
        with mock.patch.object(product_image_views.pl, 'read_library_settings', return_value=('/lib', 2)), \
                mock.patch.object(product_image_views.pl, 'load_library', return_value=['pose']), \
                mock.patch.object(product_image_views.svc, 'read_rembg_model', return_value='isnet-general-use'), \
                mock.patch.object(product_image_views.det, 'read_detector_model_path', return_value='/x.onnx'), \
                mock.patch.object(product_image_views.det, 'get_detector', return_value=detector), \
                mock.patch.object(product_image_views.hs, 'read_swap_paths', return_value=('/p.onnx', '/m')), \
                mock.patch.object(product_image_views.hs, 'get_models', return_value=swap_models), \
                mock.patch.object(product_image_views.svc, 'get_rembg_session', return_value=object()), \
                mock.patch.object(product_image_views.svc, 'cut_out_product', return_value=(cutout.getvalue(), 10, 20, [0, 5, 10, 20])), \
                mock.patch.object(product_image_views.pl, 'compose_best', return_value=results) as compose:
            response = self._call(self._image())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['usage']['cost_usd'], 0)
        self.assertEqual([c['info']['pose_id'] for c in response.data['candidates']], ['p1', 'p2'])
        self.assertEqual(base64.b64decode(response.data['candidates'][0]['image_base64']), b'jpeg-a')
        self.assertEqual(compose.call_args[0][3], [0, 5, 10, 20])  # khung thân SP mới từ bước tách nền
        self.assertEqual(compose.call_args[0][4], 2)               # số ảnh theo PRODUCT_IMAGE_POSE_CANDIDATES
        self.assertIsNone(compose.call_args[0][5])                 # không nhập cm → ghép theo cỡ SP cũ như trước

    def test_width_cm_is_passed_and_validated(self):
        files = {**self._image(), 'width_cm': '24,5'}
        with mock.patch.object(product_image_views.pl, 'read_library_settings', return_value=('/lib', 1)), \
                mock.patch.object(product_image_views.pl, 'load_library', return_value=['pose']), \
                mock.patch.object(product_image_views.svc, 'read_rembg_model', return_value='isnet-general-use'), \
                mock.patch.object(product_image_views.det, 'read_detector_model_path', return_value='/x.onnx'), \
                mock.patch.object(product_image_views.det, 'get_detector', return_value=mock.Mock()), \
                mock.patch.object(product_image_views.hs, 'read_swap_paths', return_value=('/p.onnx', '/m')), \
                mock.patch.object(product_image_views.hs, 'get_models', return_value={'person': mock.Mock()}), \
                mock.patch.object(product_image_views.svc, 'get_rembg_session', return_value=object()), \
                mock.patch.object(product_image_views.svc, 'cut_out_product', return_value=(self._cutout(), 10, 20, None)), \
                mock.patch.object(product_image_views.pl, 'compose_best', return_value=[]) as compose:
            self.assertEqual(self._call(files).status_code, 200)
        self.assertEqual(compose.call_args[0][5], 24.5)
        for bad in ('abc', '3', '75'):
            response = self._call({**self._image(), 'width_cm': bad})
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn('cm', response.data['error_message'])

    @staticmethod
    def _cutout():
        buf = io.BytesIO(); Image.new('RGBA', (10, 20), (200, 0, 0, 255)).save(buf, 'PNG')
        return buf.getvalue()
