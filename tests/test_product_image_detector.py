"""Tạo ảnh sản phẩm — nhận diện túi bằng model tự huấn luyện (ONNX, 0đ) + tự kiểm khi không có đáp án.

Khoá phần thuần logic quanh model: đọc biến môi trường + metadata file model, đổi khung từ ảnh vuông của
model về ảnh gốc (kể cả ảnh lật), tự kiểm 3 lần dự đoán, phát hiện khung túi hụt quai/dây bằng mặt nạ
rembg, và đổi kết quả sang dạng isolate_product dùng. Model thật được thay bằng session giả — độ chính xác
của model chỉ đo được bằng ảnh thật (đã đo trên ảnh có nhãn Gemini và ảnh dựng có mặt nạ đáp án).

Chạy: python manage.py test tests.test_product_image_detector
"""
import json
import os
import sys
import tempfile
import types
from unittest import mock

# Nạp scipy.ndimage TRƯỚC các mock.patch.dict(sys.modules, ...) bên dưới: patch.dict khôi phục sys.modules bằng cách
# gỡ mọi module nạp trong lúc patch → lần sau import lại phần mở rộng C của scipy thì báo "cannot load module more
# than once per process" (hỏng cả test chạy sau, 13/10).
import scipy.ndimage  # noqa: F401
import numpy as np
from django.test import SimpleTestCase
from PIL import Image

from video_management.services import product_detector as det
from video_management.services.product_image_service import ProductImageConfigError

CLASSES = ['bag', 'bag_body', 'stand', 'other_bag', 'prop']


def _view(bag=(10, 10, 50, 60, 0.9), body=(12, 30, 48, 60, 0.9), stand=None, **others):
    view = {'bag': [bag] if bag else [], 'bag_body': [body] if body else [], 'stand': [stand] if stand else []}
    view.update({k: v for k, v in others.items()})
    return view


def _row(cx, cy, w, h, obj, cls_index, score=1.0):
    row = np.zeros(5 + len(CLASSES), np.float32)
    row[:5] = (cx, cy, w, h, obj)
    row[5 + cls_index] = score
    return row


class ReadDetectorModelPathTests(SimpleTestCase):
    def test_thieu_bien_thi_bao_ten_bien(self):
        with mock.patch.dict(os.environ, {'PRODUCT_IMAGE_DETECTOR_MODEL_PATH': ''}):
            with self.assertRaisesMessage(ProductImageConfigError, 'PRODUCT_IMAGE_DETECTOR_MODEL_PATH'):
                det.read_detector_model_path()

    def test_file_khong_ton_tai_bao_ro_duong_dan(self):
        with mock.patch.dict(os.environ, {'PRODUCT_IMAGE_DETECTOR_MODEL_PATH': '/khong/co/model.onnx'}):
            with self.assertRaisesMessage(ProductImageConfigError, '/khong/co/model.onnx'):
                det.read_detector_model_path()

    def test_doc_dung_duong_dan(self):
        with tempfile.NamedTemporaryFile(suffix='.onnx') as f:
            with mock.patch.dict(os.environ, {'PRODUCT_IMAGE_DETECTOR_MODEL_PATH': f' {f.name} '}):
                self.assertEqual(det.read_detector_model_path(), f.name)


class GetDetectorTests(SimpleTestCase):
    def setUp(self):
        det._detectors.clear()
        self.addCleanup(det._detectors.clear)
        self.meta = {'classes': json.dumps(CLASSES), 'input_size': '512'}
        session = mock.Mock()
        session.get_modelmeta.return_value.custom_metadata_map = self.meta
        session.get_inputs.return_value = [mock.Mock(name='input')]
        fake_ort = types.ModuleType('onnxruntime')
        fake_ort.SessionOptions = mock.Mock
        fake_ort.InferenceSession = mock.Mock(return_value=session)
        self.ort = fake_ort
        patcher = mock.patch.dict(sys.modules, {'onnxruntime': fake_ort})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_doc_lop_va_co_anh_tu_metadata_va_giu_lai_session(self):
        detector = det.get_detector('/models/bag-detector-run-1.onnx')
        self.assertEqual((detector.classes, detector.size, detector.name), (CLASSES, 512, 'bag-detector-run-1'))
        self.assertIs(det.get_detector('/models/bag-detector-run-1.onnx'), detector)
        self.ort.InferenceSession.assert_called_once()

    def test_model_thieu_lop_bat_buoc_bao_loi_cau_hinh(self):
        self.meta['classes'] = json.dumps(['bag', 'other_bag'])
        with self.assertRaisesMessage(ProductImageConfigError, "['bag_body', 'stand']"):
            det.get_detector('/models/x.onnx')

    def test_file_khong_phai_model_nhan_dien_bao_ten_bien(self):
        self.ort.InferenceSession.side_effect = RuntimeError('INVALID_PROTOBUF')
        with self.assertRaisesMessage(ProductImageConfigError, 'PRODUCT_IMAGE_DETECTOR_MODEL_PATH'):
            det.get_detector('/models/hong.onnx')


class LetterboxAndDecodeTests(SimpleTestCase):
    def test_letterbox_dat_anh_giua_nen_114_va_doi_sang_bgr(self):
        tensor, (ratio, left, top) = det.letterbox(Image.new('RGB', (200, 100), (255, 0, 0)), 64)
        self.assertEqual(tensor.shape, (1, 3, 64, 64))
        self.assertEqual((ratio, left, top), (0.32, 0, 16))
        self.assertEqual(tensor[0, :, 0, 0].tolist(), [114, 114, 114])      # lề trên
        self.assertEqual(tensor[0, :, 32, 32].tolist(), [0, 0, 255])         # đỏ → BGR

    def test_doi_khung_ve_anh_goc_va_chi_lay_mot_tui(self):
        # Ảnh 200×100 ở ảnh vuông 64: tỉ lệ 0,32, lề trên 16.
        output = np.stack([
            _row(32, 32, 32, 16, 0.9, 0),            # túi: x 16..48, y 24..40 → gốc x 50..150, y 25..75
            _row(30, 32, 30, 14, 0.8, 0),            # túi thứ hai điểm thấp hơn — bỏ
            _row(32, 34, 20, 10, 0.9, 1),            # thân
            _row(10, 30, 8, 8, 0.4, 3),              # túi khác điểm 0,4 < 0,5 — bỏ
        ])
        boxes = det.decode(output, CLASSES, (0.32, 0, 16), 200, 100)
        self.assertEqual(len(boxes['bag']), 1)
        np.testing.assert_allclose(boxes['bag'][0][:4], (50, 25, 150, 75), atol=1e-4)
        self.assertEqual(len(boxes['bag_body']), 1)
        self.assertEqual(boxes['other_bag'], [])

    def test_anh_lat_doi_khung_ve_dung_ben(self):
        output = np.stack([_row(16, 32, 16, 16, 0.9, 0)])    # x 8..24 → gốc 25..75 trên ảnh lật
        boxes = det.decode(output, CLASSES, (0.32, 0, 16), 200, 100, flip=True)
        np.testing.assert_allclose(boxes['bag'][0][:4], (125, 25, 175, 75), atol=1e-4)

    def test_nhieu_manh_ke_giu_ca_hai_bo_khung_trung(self):
        output = np.stack([
            _row(20, 50, 10, 6, 0.9, 2), _row(20, 50, 10, 6, 0.8, 2),  # trùng → giữ 1
            _row(45, 50, 10, 6, 0.7, 2),                                # mảnh khác
        ])
        boxes = det.decode(output, CLASSES, (1.0, 0, 0), 64, 64)
        self.assertEqual(len(boxes['stand']), 2)


class SelfCheckTests(SimpleTestCase):
    def test_ba_lan_khop_nhau_thi_tu_tin(self):
        agreement, reasons = det.self_check([_view(), _view(), _view(bag=(11, 10, 50, 61, 0.9))])
        self.assertEqual(reasons, [])
        self.assertGreater(agreement['bag'], 0.9)

    def test_khung_tui_lech_giua_cac_lan_thi_khong_chac(self):
        _, reasons = det.self_check([_view(), _view(bag=(10, 30, 50, 60, 0.9)), _view()])
        self.assertTrue(any('khung túi 3 lần lệch nhau' in r for r in reasons))

    def test_ke_luc_thay_luc_khong_thi_khong_chac(self):
        _, reasons = det.self_check([_view(stand=(0, 60, 64, 64, 0.5)), _view(), _view()])
        self.assertIn('kệ/hộp lúc thấy lúc không', reasons)

    def test_khong_thay_than_hoac_do_tin_thap(self):
        _, reasons = det.self_check([_view(body=None, bag=(10, 10, 50, 60, 0.4))] * 3)
        self.assertIn('không thấy thân túi', reasons)
        self.assertIn('túi độ tin thấp (40%)', reasons)

    def test_than_tui_loi_ra_ngoai_khung_tui(self):
        _, reasons = det.self_check([_view(body=(0, 30, 60, 60, 0.9))] * 3)
        self.assertIn('thân túi lòi ra ngoài khung túi', reasons)


class OutsideMaskTests(SimpleTestCase):
    def setUp(self):
        self.alpha = np.zeros((100, 100), np.uint8)
        self.alpha[40:80, 30:70] = 255     # thân túi

    def test_quai_vuot_len_tren_khung_tui_bi_bat(self):
        self.alpha[5:40, 33:37] = 255      # quai chạy lên quá khung túi (khung bắt đầu ở y=38)
        self.alpha[5:40, 63:67] = 255
        mask, fraction = det.outside_mask(self.alpha, _view(bag=(30, 38, 70, 80, 0.9)))
        self.assertGreater(fraction, det.OUTSIDE_MASK_MAX_FRACTION)
        self.assertTrue(mask[10, 35] and mask[10, 65])      # đúng hai quai
        self.assertEqual(int(mask.sum()), 2 * 31 * 4)

    def test_ke_duoi_day_nam_trong_khung_ke_khong_tinh(self):
        self.alpha[80:100, 10:90] = 255    # hộp gỗ dưới đáy, liền với túi
        main = _view(bag=(30, 38, 70, 80, 0.9), stand=(8, 80, 92, 100, 0.9))
        mask, fraction = det.outside_mask(self.alpha, main)
        self.assertEqual((int(mask.sum()), fraction), (0, 0.0))

    def test_vun_nen_roi_rac_khong_tinh(self):
        self.alpha[2:10, 2:10] = 255       # vật xa, không nối với túi
        mask, fraction = det.outside_mask(self.alpha, _view(bag=(30, 38, 70, 80, 0.9)))
        self.assertEqual((int(mask.sum()), fraction), (0, 0.0))

    def test_detect_product_them_ly_do_khi_khung_hut_quai(self):
        self.alpha[5:40, 33:37] = 255
        self.alpha[5:40, 63:67] = 255
        view = _view(bag=(30, 38, 70, 80, 0.9), body=(30, 40, 70, 80, 0.9))
        with mock.patch.object(det, 'predict', return_value=view):
            result = det.detect_product(mock.Mock(), Image.new('RGB', (100, 100)), self.alpha)
        self.assertFalse(result.confident)
        self.assertTrue(result.reasons[-1].startswith('phần túi chạy ra ngoài khung'))

    def test_detect_product_tu_tin_khi_moi_thu_khop(self):
        view = _view(bag=(30, 38, 70, 80, 0.9), body=(30, 40, 70, 80, 0.9))
        with mock.patch.object(det, 'predict', return_value=view) as predict:
            result = det.detect_product(mock.Mock(), Image.new('RGB', (100, 100)), self.alpha)
        self.assertTrue(result.confident)
        self.assertEqual([c.kwargs for c in predict.call_args_list], [{}, {'flip': True}, {'scale': det.SHRUNK_VIEW_SCALE}])


class ToLocatedTests(SimpleTestCase):
    def test_doi_sang_dang_isolate_product(self):
        main = _view(bag=(10.4, 10.6, 50.2, 60.7, 0.9), body=(5, 30, 48, 70, 0.9), stand=(0, 60, 64, 64, 0.8),
                     other_bag=[(70, 0, 90, 20, 0.6)], prop=[])
        keep = np.zeros((64, 64), bool)
        product, props, body, keep_outside = det.to_located(main, keep)
        self.assertIs(keep_outside, keep)
        self.assertEqual(product, (10, 10, 51, 61))
        self.assertEqual(props, [(0, 60, 64, 64), (70, 0, 90, 20)])
        self.assertEqual(body, (10, 30, 48, 61))       # kẹp vào trong khung túi


class KeepOutsideInCutoutTests(SimpleTestCase):
    def test_quai_hut_khung_it_van_duoc_giu_trong_anh_ket_qua(self):
        # Model khoanh túi từ y=38 nhưng quai chạy lên tới y=33 (ít — dưới ngưỡng chuyển Gemini): ảnh kết
        # quả phải còn nguyên quai, không cắt cụt ở mép khung.
        import io
        from video_management.services import product_image_service as svc

        def remover(image):
            out = Image.new('RGBA', image.size, (0, 0, 0, 0))
            out.paste((10, 120, 200, 255), (30, 40, 70, 80))    # thân
            out.paste((10, 120, 200, 255), (33, 33, 37, 40))    # quai trái lên quá khung
            out.paste((10, 120, 200, 255), (63, 33, 67, 40))    # quai phải
            out.paste((40, 40, 40, 255), (10, 80, 90, 100))     # hộp gỗ dưới đáy
            return out
        view = _view(bag=(30, 38, 70, 80, 0.9), body=(30, 40, 70, 80, 0.9), stand=(8, 80, 92, 100, 0.9))
        buffer = io.BytesIO()
        Image.new('RGB', (100, 100), 'white').save(buffer, format='PNG')

        def locate(image, alpha):
            with mock.patch.object(det, 'predict', return_value=view):
                result = det.detect_product(mock.Mock(), image, alpha)
            self.assertTrue(result.confident)
            return det.to_located(result.main, result.keep_outside)
        png, width, height, _ = svc.cut_out_product(buffer.getvalue(), remover, locate)
        self.assertEqual((width, height), (40, 47))          # cao 33..80: còn quai, hộp gỗ đã bỏ


class EnsembleConfigTests(SimpleTestCase):
    """Gộp 2 model (15/10): biến môi trường ghi "<model chính>,<model phụ>"."""

    def test_two_paths_return_primary_then_secondary(self):
        with tempfile.NamedTemporaryFile(suffix='.onnx') as a, tempfile.NamedTemporaryFile(suffix='.onnx') as b:
            with mock.patch.dict(os.environ, {'PRODUCT_IMAGE_DETECTOR_MODEL_PATH': f'{a.name}, {b.name}'}):
                self.assertEqual(det.read_detector_model_path(), (a.name, b.name))

    def test_more_than_two_models_is_config_error(self):
        with tempfile.NamedTemporaryFile(suffix='.onnx') as a:
            with mock.patch.dict(os.environ, {'PRODUCT_IMAGE_DETECTOR_MODEL_PATH': f'{a.name},{a.name},{a.name}'}):
                with self.assertRaisesMessage(ProductImageConfigError, 'tối đa 2'):
                    det.read_detector_model_path()

    def test_missing_second_model_names_its_path(self):
        with tempfile.NamedTemporaryFile(suffix='.onnx') as a:
            with mock.patch.dict(os.environ, {'PRODUCT_IMAGE_DETECTOR_MODEL_PATH': f'{a.name},/khong/co/phu.onnx'}):
                with self.assertRaisesMessage(ProductImageConfigError, '/khong/co/phu.onnx'):
                    det.read_detector_model_path()


class EnsembleDetectorTests(GetDetectorTests):
    def test_tuple_path_loads_both_models_once_and_names_them_shortly(self):
        paths = ('/models/bag-detector-F4_bal.onnx', '/models/bag-detector-F_t512_e25_L1327.onnx')
        ensemble = det.get_detector(paths)
        self.assertIsInstance(ensemble, det.EnsembleDetector)
        self.assertEqual([m.name for m in ensemble.members], ['bag-detector-F4_bal', 'bag-detector-F_t512_e25_L1327'])
        # Cột model của nhật ký chi phí là VarChar(100) — tên gộp bỏ tiền tố chung.
        self.assertEqual(ensemble.name, 'bag-detector-F4_bal+F_t512_e25_L1327')
        self.assertIs(det.get_detector(paths).members[0], ensemble.members[0])
        self.assertEqual(self.ort.InferenceSession.call_count, 2)


class FusePredictionsTests(SimpleTestCase):
    def test_agreeing_boxes_are_averaged_by_score(self):
        fused = det.fuse_predictions({'bag': [(0, 0, 100, 100, 0.9)]}, {'bag': [(10, 0, 110, 100, 0.3)]})
        x0, y0, x1, y1, score = fused['bag'][0]
        self.assertAlmostEqual(x0, 2.5)                    # (0×0,9 + 10×0,3) / 1,2
        self.assertAlmostEqual(x1, 102.5)
        self.assertEqual(score, 0.9)

    def test_disagreeing_bag_trusts_primary_model(self):
        # Model ảnh shop cũ khoanh cả bức ảnh web (soát 15/10) — lệch nhau thì theo model chính.
        primary, secondary = {'bag': [(40, 40, 60, 60, 0.6)]}, {'bag': [(0, 0, 100, 100, 0.95)]}
        self.assertEqual(det.fuse_predictions(primary, secondary)['bag'], [(40, 40, 60, 60, 0.6)])

    def test_bag_seen_only_by_secondary_is_kept(self):
        fused = det.fuse_predictions({'bag': [], 'bag_body': [(1, 1, 5, 5, 0.7)]}, {'bag': [(0, 0, 9, 9, 0.8)], 'bag_body': []})
        self.assertEqual(fused['bag'], [(0, 0, 9, 9, 0.8)])
        self.assertEqual(fused['bag_body'], [(1, 1, 5, 5, 0.7)])

    def test_stands_matched_averaged_unmatched_secondary_needs_high_score(self):
        primary = {'stand': [(0, 80, 100, 100, 0.8), (200, 200, 220, 220, 0.55)]}
        secondary = {'stand': [(0, 82, 100, 100, 0.8), (300, 300, 320, 320, 0.4), (400, 400, 420, 420, 0.65)]}
        fused = det.fuse_predictions(primary, secondary)['stand']
        self.assertEqual(len(fused), 3)
        self.assertAlmostEqual(fused[0][1], 81.0)                       # kệ trùng nhau → trung bình
        self.assertIn((400, 400, 420, 420, 0.65), fused)                # chỉ model phụ thấy, độ tin ≥ 0,6 → giữ
        self.assertNotIn((300, 300, 320, 320, 0.4), fused)              # chỉ model phụ thấy, độ tin thấp → bỏ (kệ bịa)
        self.assertIn((200, 200, 220, 220, 0.55), fused)                # chỉ model chính thấy → giữ
        self.assertEqual([b[4] for b in fused], sorted((b[4] for b in fused), reverse=True))


class PredictEnsembleTests(SimpleTestCase):
    def _detector(self, rows, name):
        session = mock.Mock()
        session.get_inputs.return_value = [mock.Mock()]
        session.run.return_value = [np.array([rows], np.float32)]
        return det.Detector(session, CLASSES, 64, name)

    def test_predict_runs_both_models_and_fuses(self):
        a = self._detector([_row(32, 32, 20, 20, 0.9, 0), _row(32, 32, 10, 10, 0.9, 1)], 'bag-detector-A')
        b = self._detector([_row(32, 33, 20, 20, 0.8, 0), _row(32, 33, 10, 10, 0.8, 1)], 'bag-detector-B')
        image = Image.new('RGB', (64, 64))
        expected = det.fuse_predictions(det.predict(a, image), det.predict(b, image))
        self.assertEqual(det.predict(det.EnsembleDetector(a, b), image), expected)
        self.assertEqual(a.session.run.call_count, 2)
