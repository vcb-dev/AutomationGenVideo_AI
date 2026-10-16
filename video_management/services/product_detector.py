"""Nhận diện túi / thân / kệ / túi khác / đạo cụ bằng model YOLOX-tiny ONNX tự huấn luyện — CPU, 0đ.

Tự kiểm mỗi ảnh: dự đoán 3 lần (gốc, lật, thu nhỏ) phải khớp, luật hình học, và quai/dây chạy ra ngoài khung túi
(đối chiếu mặt nạ rembg). Model chưa chắc thì lọc thận trọng (decide_cutout).
Biến DETECTOR_MODEL_ENV bắt buộc; ghi "<model chính>,<model phụ>" để gộp 2 model. Lớp và cỡ ảnh đọc từ metadata ONNX.
"""
import json
import os
import threading

import numpy as np
from PIL import Image

from video_management.services.product_image_service import (
    OPAQUE,
    PRODUCT_BOX_MARGIN,
    ProductImageConfigError,
    isolate_product,
)

DETECTOR_MODEL_ENV = 'PRODUCT_IMAGE_DETECTOR_MODEL_PATH'
REQUIRED_CLASSES = ('bag', 'bag_body', 'stand')
CLASS_NAMES_VI = {'bag': 'túi', 'bag_body': 'thân túi', 'stand': 'kệ/hộp', 'other_bag': 'túi khác', 'prop': 'đạo cụ'}

MAIN_CLASS_MIN_SCORE = 0.25
OTHER_CLASS_MIN_SCORE = 0.5
NMS_IOU = 0.45
LETTERBOX_PAD = 114
# IoU tối thiểu giữa 3 lần dự đoán.
VIEW_AGREEMENT = {'bag': 0.85, 'bag_body': 0.85, 'stand': 0.75}
SHRUNK_VIEW_SCALE = 0.8
MIN_MAIN_SCORE = 0.6
BODY_INSIDE_FRACTION = 0.9
# Phần túi ngoài khung quá tỉ lệ này → khung túi hụt quai/dây.
OUTSIDE_MASK_MAX_FRACTION = 0.03
# Model chưa chắc mà phần dính liền nghi là kệ quá tỉ lệ này → báo người dùng xem lại.
UNSURE_ATTACHED_WARN_FRACTION = 0.02
SEPARATE_OBJECT_MIN_FRACTION = 0.0005
# Gộp 2 model: model chính thắng khi lệch; khung trùng thì lấy trung bình; khung chỉ model phụ thấy thì giữ (kệ/đạo cụ
# cần độ tin ≥ SECONDARY_EXTRA_MIN_SCORE).
MAX_ENSEMBLE_MODELS = 2
ENSEMBLE_AGREE_IOU = 0.7
SECONDARY_EXTRA_MIN_SCORE = 0.6


_detectors = {}
_detectors_lock = threading.Lock()


def read_detector_model_path():
    """Đường dẫn file model (str), hoặc (model chính, model phụ) khi biến ghi 2 đường dẫn cách nhau dấu phẩy."""
    paths = [p.strip() for p in (os.environ.get(DETECTOR_MODEL_ENV) or '').split(',') if p.strip()]
    if not paths:
        raise ProductImageConfigError(
            f'{DETECTOR_MODEL_ENV} chưa được set trên AI Service (đường dẫn file ONNX model nhận diện túi tự '
            f'huấn luyện — nhận diện 0đ; gộp 2 model thì ghi "<model chính>,<model phụ>").'
        )
    if len(paths) > MAX_ENSEMBLE_MODELS:
        raise ProductImageConfigError(f'{DETECTOR_MODEL_ENV} ghi {len(paths)} model — tối đa {MAX_ENSEMBLE_MODELS}.')
    for path in paths:
        if not os.path.isfile(path):
            raise ProductImageConfigError(f'{DETECTOR_MODEL_ENV}={path} không tồn tại trên AI Service.')
    return paths[0] if len(paths) == 1 else tuple(paths)


class Detector:
    """Session ONNX + lớp/cỡ ảnh đọc từ metadata file model."""

    def __init__(self, session, classes, size, name):
        self.session = session
        self.classes = classes
        self.size = size
        self.name = name
        self.input_name = session.get_inputs()[0].name


class EnsembleDetector:
    """Hai model gộp (fuse_predictions): `members` = (model chính, model phụ)."""

    def __init__(self, primary, secondary):
        self.members = (primary, secondary)
        # Tên ngắn cho nhật ký: bỏ tiền tố chung.
        common = os.path.commonprefix([primary.name, secondary.name])
        common = common[:common.rfind('-') + 1]
        self.name = f'{primary.name}+{secondary.name[len(common):]}'


def get_detector(path):
    """Nạp model một lần mỗi worker; `path` là tuple 2 đường dẫn → gộp 2 model."""
    if isinstance(path, (tuple, list)):
        return EnsembleDetector(*(_load_detector(p) for p in path))
    return _load_detector(path)


def _load_detector(path):
    with _detectors_lock:
        detector = _detectors.get(path)
        if detector is None:
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.enable_cpu_mem_arena = False
            options.enable_mem_pattern = False
            try:
                session = ort.InferenceSession(path, sess_options=options, providers=['CPUExecutionProvider'])
                meta = session.get_modelmeta().custom_metadata_map
                classes = json.loads(meta['classes'])
                size = int(meta['input_size'])
            except Exception as e:
                raise ProductImageConfigError(f'{DETECTOR_MODEL_ENV}={path} không phải model nhận diện hợp lệ: {e}')
            missing = [c for c in REQUIRED_CLASSES if c not in classes]
            if missing:
                raise ProductImageConfigError(f'{DETECTOR_MODEL_ENV}={path} thiếu lớp {missing} (có {classes}).')
            name = os.path.splitext(os.path.basename(path))[0]
            detector = _detectors[path] = Detector(session, classes, size, name)
        return detector


def box_iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _nms(boxes, scores, iou_threshold):
    """Chỉ số khung giữ lại, xếp theo độ tin giảm dần."""
    order = list(np.argsort(-scores))
    keep = []
    while order:
        i = order.pop(0)
        keep.append(i)
        order = [j for j in order if box_iou(boxes[i], boxes[j]) < iou_threshold]
    return keep


def letterbox(image, size, flip=False, scale=1.0):
    """Ảnh RGB → mảng (1, 3, size, size) BGR 0–255 như lúc huấn luyện, kèm (tỉ lệ, lề trái, lề trên)."""
    if flip:
        image = image.transpose(Image.FLIP_LEFT_RIGHT)
    width, height = image.size
    ratio = size / max(width, height) * scale
    new_w, new_h = max(1, int(width * ratio)), max(1, int(height * ratio))
    left, top = (size - new_w) // 2, (size - new_h) // 2
    canvas = np.full((size, size, 3), LETTERBOX_PAD, np.float32)
    canvas[top:top + new_h, left:left + new_w] = np.asarray(image.convert('RGB').resize((new_w, new_h), Image.BILINEAR), np.float32)[:, :, ::-1]
    return canvas.transpose(2, 0, 1)[None], (ratio, left, top)


def decode(output, classes, transform, width, height, flip=False):
    """Đầu ra model (N, 5 + lớp) → {lớp: [(x0, y0, x1, y1, độ tin)]} theo pixel ảnh gốc, độ tin giảm dần.
    Túi và thân túi chỉ lấy một khung (túi chính)."""
    ratio, left, top = transform
    xyxy = np.stack([
        output[:, 0] - output[:, 2] / 2, output[:, 1] - output[:, 3] / 2,
        output[:, 0] + output[:, 2] / 2, output[:, 1] + output[:, 3] / 2,
    ], axis=1)
    xyxy = (xyxy - np.array([left, top, left, top], np.float32)) / ratio
    result = {}
    for c, name in enumerate(classes):
        scores = output[:, 4] * output[:, 5 + c]
        threshold = MAIN_CLASS_MIN_SCORE if name in REQUIRED_CLASSES else OTHER_CLASS_MIN_SCORE
        idx = np.flatnonzero(scores > threshold)
        keep = [idx[k] for k in _nms(xyxy[idx], scores[idx], NMS_IOU)] if len(idx) else []
        if name in ('bag', 'bag_body'):
            keep = keep[:1]
        boxes = []
        for i in keep:
            x0, y0, x1, y1 = (float(v) for v in xyxy[i])
            x0, x1, y0, y1 = max(0.0, x0), min(float(width), x1), max(0.0, y0), min(float(height), y1)
            if flip:
                x0, x1 = width - x1, width - x0
            if x1 > x0 and y1 > y0:
                boxes.append((x0, y0, x1, y1, float(scores[i])))
        result[name] = boxes
    return result


def _weighted_box(a, b):
    total = a[4] + b[4]
    return tuple((a[i] * a[4] + b[i] * b[4]) / total for i in range(4)) + (max(a[4], b[4]),)


def fuse_predictions(primary, secondary):
    """Gộp dự đoán model chính + model phụ — luật ở chú thích ENSEMBLE_*."""
    out = {}
    for name in set(primary) | set(secondary):
        mains, extras = primary.get(name, []), secondary.get(name, [])
        if name in ('bag', 'bag_body'):
            a, b = (mains or [None])[0], (extras or [None])[0]
            if a and b:
                out[name] = [_weighted_box(a, b) if box_iou(a, b) >= ENSEMBLE_AGREE_IOU else a]
            else:
                out[name] = [a or b] if (a or b) else []
            continue
        used, fused = set(), []
        for b in extras:
            match = max(((box_iou(a, b), j) for j, a in enumerate(mains) if j not in used), default=(0.0, None))
            if match[1] is not None and match[0] >= ENSEMBLE_AGREE_IOU:
                used.add(match[1])
                fused.append(_weighted_box(mains[match[1]], b))
            elif b[4] >= SECONDARY_EXTRA_MIN_SCORE:
                fused.append(b)
        fused += [a for j, a in enumerate(mains) if j not in used]
        out[name] = sorted(fused, key=lambda box: -box[4])
    return out


def predict(detector, image, flip=False, scale=1.0):
    if isinstance(detector, EnsembleDetector):
        primary, secondary = (predict(m, image, flip, scale) for m in detector.members)
        return fuse_predictions(primary, secondary)
    tensor, transform = letterbox(image, detector.size, flip, scale)
    output = detector.session.run(None, {detector.input_name: tensor})[0][0]
    return decode(output, detector.classes, transform, *image.size, flip=flip)


def self_check(views):
    """(độ khớp từng lớp, [lý do không chắc]) từ dự đoán ảnh gốc + các biến thể."""
    main = views[0]
    agreement, reasons = {}, []
    for name, threshold in VIEW_AGREEMENT.items():
        present = [bool(v.get(name)) for v in views]
        if not any(present):
            continue
        if not all(present):
            agreement[name] = 0.0
            reasons.append(f'{CLASS_NAMES_VI[name]} lúc thấy lúc không')
            continue
        agreement[name] = min(box_iou(main[name][0], v[name][0]) for v in views[1:])
        if agreement[name] < threshold:
            reasons.append(f'khung {CLASS_NAMES_VI[name]} 3 lần lệch nhau ({agreement[name] * 100:.0f}%)')
    for name in ('bag', 'bag_body'):
        if not main.get(name):
            reasons.append(f'không thấy {CLASS_NAMES_VI[name]}')
        elif main[name][0][4] < MIN_MAIN_SCORE:
            reasons.append(f'{CLASS_NAMES_VI[name]} độ tin thấp ({main[name][0][4] * 100:.0f}%)')
    if main.get('bag') and main.get('bag_body'):
        bag, body = main['bag'][0], main['bag_body'][0]
        inter = max(0.0, min(bag[2], body[2]) - max(bag[0], body[0])) * max(0.0, min(bag[3], body[3]) - max(bag[1], body[1]))
        if inter < BODY_INSIDE_FRACTION * (body[2] - body[0]) * (body[3] - body[1]):
            reasons.append('thân túi lòi ra ngoài khung túi')
    return agreement, reasons


def outside_mask(alpha, main):
    """(mặt nạ, tỉ lệ) phần SP nối liền túi nằm ngoài khung túi và ngoài khung kệ/đạo cụ — quai/dây model khoanh hụt."""
    from scipy import ndimage

    height, width = alpha.shape
    x0, y0, x1, y1 = (int(round(v)) for v in main['bag'][0][:4])
    pad = round(PRODUCT_BOX_MARGIN * max(width, height))
    opaque = alpha >= OPAQUE
    inside = np.zeros_like(opaque)
    inside[max(0, y0 - pad):min(height, y1 + pad), max(0, x0 - pad):min(width, x1 + pad)] = True
    explained = inside.copy()
    for name in ('stand', 'other_bag', 'prop'):
        for bx0, by0, bx1, by1, _ in main.get(name, []):
            explained[int(by0):int(np.ceil(by1)), int(bx0):int(np.ceil(bx1))] = True
    labels, _ = ndimage.label(opaque & ~(explained & ~inside), structure=np.ones((3, 3)))
    touching = np.unique(labels[opaque & inside])
    outside = np.isin(labels, touching[touching > 0]) & ~explained
    in_box = int((opaque & inside).sum())
    return outside, (float(outside.sum()) / in_box if in_box else 0.0)


class DetectionResult:
    def __init__(self, main, agreement, reasons, keep_outside=None):
        self.main = main
        self.agreement = agreement
        self.reasons = reasons
        self.keep_outside = keep_outside

    @property
    def confident(self):
        return not self.reasons


def detect_product(detector, image, alpha):
    """Nhận diện + tự kiểm. `alpha` là mặt nạ rembg cùng cỡ ảnh (mảng 2 chiều)."""
    views = [predict(detector, image), predict(detector, image, flip=True), predict(detector, image, scale=SHRUNK_VIEW_SCALE)]
    agreement, reasons = self_check(views)
    main = views[0]
    keep_outside = None
    if main.get('bag'):
        keep_outside, fraction = outside_mask(np.asarray(alpha), main)
        if fraction > OUTSIDE_MASK_MAX_FRACTION:
            reasons.append(f'phần túi chạy ra ngoài khung (quai/dây? {fraction * 100:.0f}%)')
    return DetectionResult(main, agreement, reasons, keep_outside)


def to_located(main, keep_outside=None):
    """(khung SP, [khung đạo cụ], khung thân | None, phần ngoài khung cần giữ | None) theo pixel nguyên."""
    def pixels(box):
        return (int(np.floor(box[0])), int(np.floor(box[1])), int(np.ceil(box[2])), int(np.ceil(box[3])))

    product = pixels(main['bag'][0])
    props = [pixels(b) for name in ('stand', 'other_bag', 'prop') for b in main.get(name, [])]
    body = None
    if main.get('bag_body'):
        bx0, by0, bx1, by1 = pixels(main['bag_body'][0])
        bx0, by0, bx1, by1 = max(bx0, product[0]), max(by0, product[1]), min(bx1, product[2]), min(by1, product[3])
        body = (bx0, by0, bx1, by1) if bx1 > bx0 and by1 > by0 else None
    return product, props, body, keep_outside


def decide_cutout(alpha, result):
    """→ (located | None, detection). Tự tin: bỏ kệ/đạo cụ theo khung. Chưa chắc: chỉ bỏ vật tách rời, báo nếu nghi
    kệ dính liền. Không thấy túi: giữ nguyên rembg."""
    from scipy import ndimage

    main = result.main
    if not main.get('bag'):
        return None, {'status': 'skipped', 'source': 'model', 'reason': 'Model không thấy túi trong ảnh — giữ nguyên ảnh tách nền.',
                      'model_reasons': result.reasons}
    located = to_located(main, result.keep_outside)
    if result.confident:
        return located, {'status': 'applied', 'source': 'model', 'props_removed': len(located[1])}

    alpha = np.asarray(alpha)
    height, width = alpha.shape
    x0, y0, x1, y1 = located[0]
    pad = round(PRODUCT_BOX_MARGIN * max(width, height))
    opaque = alpha >= OPAQUE
    labels, _ = ndimage.label(opaque, structure=np.ones((3, 3)))
    zone = labels[max(0, y0 - pad):min(height, y1 + pad), max(0, x0 - pad):min(width, x1 + pad)]
    touching = np.unique(zone[zone > 0])
    keep = np.isin(labels, touching)
    separate = [n for n in np.unique(labels[opaque & ~keep]) if (labels == n).sum() >= SEPARATE_OBJECT_MIN_FRACTION * alpha.size]
    conservative = (located[0], [], located[2], keep)

    full = isolate_product(alpha, located[0], located[1])
    if located[3] is not None:
        full = np.where(located[3], alpha, full)
    kept = keep & opaque
    attached = float((kept & (full < OPAQUE)).sum()) / max(1, int(kept.sum()))
    detection = {'status': 'applied', 'source': 'model', 'props_removed': len(separate), 'model_reasons': result.reasons}
    if attached > UNSURE_ATTACHED_WARN_FRACTION:
        detection = {
            'status': 'skipped', 'source': 'model', 'model_reasons': result.reasons,
            'reason': f'Model chưa chắc ({"; ".join(result.reasons)}) nên chưa cắt phần dính liền túi mà model nghi là '
                      f'kệ/đạo cụ ({attached * 100:.0f}% ảnh sản phẩm) — xem lại ảnh này.',
        }
    return conservative, detection
