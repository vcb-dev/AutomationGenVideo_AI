"""Đổi SP chị Nhạm đang cầm sang SP mới — 0đ, chạy CPU.

  1. model người cầm SP (ONNX) → khung SP cũ / thân / từng bàn tay
  2. MobileSAM → mặt nạ SP cũ (cả quai) và bàn tay
  3. LaMa → xoá SP cũ, vẽ lại nền
  4. đặt SP mới theo cỡ thật, nhuộm ấm theo cảnh, bóng đổ; dán lại ngón tay lên trên
Chỉ dùng numpy/scipy/Pillow (image production không có OpenCV).

Biến môi trường bắt buộc: PERSON_DETECTOR_MODEL_ENV (model người cầm SP), SWAP_MODELS_DIR_ENV (thư mục SAM/LaMa)."""
import io
import json
import os
import threading

import numpy as np
from PIL import Image

from video_management.services import product_detector as det
from video_management.services.product_image_service import ProductImageConfigError, ProductImageInputError

PERSON_DETECTOR_MODEL_ENV = 'PRODUCT_IMAGE_PERSON_DETECTOR_MODEL_PATH'
SWAP_MODELS_DIR_ENV = 'PRODUCT_IMAGE_SWAP_MODELS_DIR'
SAM_ENCODER_FILE = 'mobile_sam_image_encoder.onnx'
SAM_DECODER_FILE = 'sam_mask_decoder_single.onnx'
SAM_MULTI_DECODER_FILE = 'sam_mask_decoder_multi.onnx'
LAMA_FILE = 'lama_fp32.onnx'
REQUIRED_PERSON_CLASSES = ('person', 'bag', 'bag_body', 'hand')

MAIN_CLASS_MIN_SCORE = 0.25
OTHER_CLASS_MIN_SCORE = 0.5
MAIN_CLASSES = ('bag', 'bag_body', 'person', 'hand')
SAM_LONG_SIDE = 1024
LAMA_SIZE = 512
MAX_SWAP_SIDE_PX = 3072
JPEG_QUALITY = 92

# Phân loại tay theo thân SP cũ (tỉ lệ chiều cao thân): nắm quai / đỡ đáy / cầm dây dài.
GRIP_BAND = 0.2
SUPPORT_BAND = 0.25
LONG_STRAP = 0.9
GRIP_IN_HAND = 0.45  # chỗ quai qua nắm tay, tính từ đỉnh khung tay
SUPPORT_OVERLAP = 0.25  # đáy SP tựa lên ngón tay đỡ
SCALE_MIN = 0.75
SCALE_MAX = 1.15
# Thước dự phòng khi không có thước khung ảnh: cạnh dài khung tay nắm / tay đỡ (cm).
GRIP_HAND_CM = 9.4
SUPPORT_HAND_CM = 16.4
GRIP_BODY_CLEAR = 0.8  # đỉnh thân SP mới không cao hơn chỗ này của khung tay nắm (nắp túi không che ngón)
GRIP_SHIFT_MAX = 0.25
# Quai cũ dưới nắm tay mà SAM sót: nhận theo màu thân SP cũ.
HANDLE_COLOR_TOL = 3.0
HANDLE_ZONE_PAD = 0.5
HANDLE_MAX_WIDTH = 0.08
HANDLE_AMBIGUOUS_FRACTION = 0.5
SKIN_SAMPLE_MIN_BAG_DIST = 1.5
HAND_PROMPT_PAD = 0.12
HAND_KEEP_PAD = 0.25
# Màu da (YCbCr) làm lõi mặt nạ tay; SAM chỉ bù mép HAND_EDGE_PX quanh lõi.
SKIN_CB = (77, 128)
SKIN_CR = (133, 180)
SKIN_MIN_LUMA = 90
HAND_EDGE_PX = 5
MIN_SKIN_FRACTION = 0.08
SAM_CORE_COVER = 0.8
SAM_MAX_GROWTH = 1.6
# Khe giữa ngón / quai cũ khác màu trong nắm tay → xoá.
GRIP_GAP_PX = 15
GRIP_GAP_FROM = 0.3
# Nhuộm ấm theo nền — cùng hệ số FE realism.
WARM_GAINS = (0.06, -0.01, -0.14)
WARM_INDEX_FULL = 0.8
SHADOW_OPACITY = 0.35
CONTACT_DARKEN = 0.25
# Lớp ngón tay nắm quai: chỉ phần ngón (tới FINGERTIP_LINE khung tay) vẽ đè lên túi mới.
GRIP_LAYER_PAD = 0.3
FINGERTIP_LINE = 0.8
# Cắt chân/cột giá trưng bày còn dính sau tách nền.
TRIM_BELOW_BODY = 0.01
STAND_ROD_BAND = 0.2
STAND_ROD_MIN_RUN = 0.5
STAND_ROD_MAX_WIDTH = 0.04
GRIP_REFILL_PAD = 0.6
# Hạt ảnh bù chi tiết cho vùng LaMa vẽ lại (LaMa chạy 512px nên mịn bệt).
GRAIN_SIGMA = 0.6
GRAIN_RING_GAP = 8
GRAIN_RING_PX = 40
GRAIN_HIGHPASS_SIGMA = 1.5
GRAIN_SEED = 20261016
GRAIN_ENOUGH = 0.9

_models = {}
_models_lock = threading.Lock()


def read_swap_paths():
    """(file model người cầm SP, thư mục model SAM/LaMa) — kiểm tồn tại, thiếu thì báo đúng tên biến/file."""
    detector = (os.environ.get(PERSON_DETECTOR_MODEL_ENV) or '').strip()
    if not detector:
        raise ProductImageConfigError(
            f'{PERSON_DETECTOR_MODEL_ENV} chưa được set trên AI Service (file ONNX model người cầm SP tự huấn luyện — '
            f'nhận diện người, SP và bàn tay để đổi SP 0đ).'
        )
    if not os.path.isfile(detector):
        raise ProductImageConfigError(f'{PERSON_DETECTOR_MODEL_ENV}={detector} không tồn tại trên AI Service.')
    folder = (os.environ.get(SWAP_MODELS_DIR_ENV) or '').strip()
    if not folder:
        raise ProductImageConfigError(
            f'{SWAP_MODELS_DIR_ENV} chưa được set trên AI Service (thư mục chứa {SAM_ENCODER_FILE}, {SAM_DECODER_FILE}, '
            f'{SAM_MULTI_DECODER_FILE}, {LAMA_FILE}).'
        )
    missing = [f for f in (SAM_ENCODER_FILE, SAM_DECODER_FILE, SAM_MULTI_DECODER_FILE, LAMA_FILE)
               if not os.path.isfile(os.path.join(folder, f))]
    if missing:
        raise ProductImageConfigError(f'{SWAP_MODELS_DIR_ENV}={folder} thiếu file {missing}.')
    return detector, folder


def _session(path):
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    return ort.InferenceSession(path, sess_options=options, providers=['CPUExecutionProvider'])


def get_models(detector_path, folder):
    """Nạp model một lần cho mỗi worker — lần sau dùng lại (như session rembg / model nhận diện SP)."""
    key = (detector_path, folder)
    with _models_lock:
        models = _models.get(key)
        if models is None:
            try:
                person = _session(detector_path)
                meta = person.get_modelmeta().custom_metadata_map
                classes = json.loads(meta['classes'])
                size = int(meta['input_size'])
            except Exception as e:
                raise ProductImageConfigError(f'{PERSON_DETECTOR_MODEL_ENV}={detector_path} không phải model nhận diện hợp lệ: {e}')
            missing = [c for c in REQUIRED_PERSON_CLASSES if c not in classes]
            if missing:
                raise ProductImageConfigError(f'{PERSON_DETECTOR_MODEL_ENV}={detector_path} thiếu lớp {missing} (có {classes}).')
            try:
                models = {
                    'person': det.Detector(person, classes, size, os.path.splitext(os.path.basename(detector_path))[0]),
                    'sam_encoder': _session(os.path.join(folder, SAM_ENCODER_FILE)),
                    'sam_decoder': _session(os.path.join(folder, SAM_DECODER_FILE)),
                    'sam_multi_decoder': _session(os.path.join(folder, SAM_MULTI_DECODER_FILE)),
                    'lama': _session(os.path.join(folder, LAMA_FILE)),
                }
            except Exception as e:
                raise ProductImageConfigError(f'{SWAP_MODELS_DIR_ENV}={folder}: không nạp được model SAM/LaMa: {e}')
            _models[key] = models
        return models


def detect(detector, image):
    """{lớp: [(x0, y0, x1, y1, độ tin)]} pixel ảnh gốc, độ tin giảm dần; SP/thân/người chỉ lấy khung tốt nhất."""
    tensor, (ratio, left, top) = det.letterbox(image, detector.size)
    out = detector.session.run(None, {detector.input_name: tensor})[0][0]
    xyxy = np.stack([out[:, 0] - out[:, 2] / 2, out[:, 1] - out[:, 3] / 2,
                     out[:, 0] + out[:, 2] / 2, out[:, 1] + out[:, 3] / 2], axis=1)
    xyxy = (xyxy - np.array([left, top, left, top], np.float32)) / ratio
    width, height = image.size
    result = {}
    for c, name in enumerate(detector.classes):
        scores = out[:, 4] * out[:, 5 + c]
        idx = np.flatnonzero(scores > (MAIN_CLASS_MIN_SCORE if name in MAIN_CLASSES else OTHER_CLASS_MIN_SCORE))
        keep = [idx[k] for k in det._nms(xyxy[idx], scores[idx], det.NMS_IOU)] if len(idx) else []
        if name in ('bag', 'bag_body', 'person'):
            keep = keep[:1]
        boxes = []
        for i in keep:
            x0, y0, x1, y1 = (float(v) for v in xyxy[i])
            x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(float(width), x1), min(float(height), y1)
            if x1 > x0 and y1 > y0:
                boxes.append((x0, y0, x1, y1, float(scores[i])))
        result[name] = boxes
    return result


class Sam:
    """MobileSAM: mã hoá ảnh một lần, rồi nhắc bằng khung/điểm → mặt nạ đúng cỡ ảnh gốc."""

    def __init__(self, models, rgb):
        h, w = rgb.shape[:2]
        self.scale = SAM_LONG_SIDE / max(h, w)
        self.hw = np.array([h, w], np.float32)
        resized = np.asarray(Image.fromarray(rgb).resize((int(round(w * self.scale)), int(round(h * self.scale))), Image.BILINEAR))
        self.models = models
        self.embedding = models['sam_encoder'].run(None, {'input_image': resized.astype(np.float32)})[0]

    def _inputs(self, points, labels):
        pts = np.asarray(points, np.float32) * self.scale
        lab = np.asarray(labels, np.float32)
        if 2 not in labels:  # chỉ có điểm (không khung) → bản xuất ONNX của SAM cần một điểm đệm nhãn -1
            pts = np.vstack([pts, [[0, 0]]]); lab = np.append(lab, -1)
        return {'image_embeddings': self.embedding, 'point_coords': pts[None].astype(np.float32),
                'point_labels': lab[None].astype(np.float32), 'mask_input': np.zeros((1, 1, 256, 256), np.float32),
                'has_mask_input': np.zeros(1, np.float32), 'orig_im_size': self.hw}

    def mask(self, points, labels):
        """labels: 1 thuộc vật, 0 không thuộc, 2/3 góc trên-trái/dưới-phải khung."""
        masks, _, _ = self.models['sam_decoder'].run(None, self._inputs(points, labels))
        return masks[0, 0] > 0

    def masks_box(self, box):
        """4 mặt nạ (một phần → cả vật) cho cùng một khung — mặt nạ lớn hay ôm được quai/dây mà bản 1 mặt nạ sót."""
        masks, _, _ = self.models['sam_multi_decoder'].run(None, self._inputs([[box[0], box[1]], [box[2], box[3]]], [2, 3]))
        return [m > 0 for m in masks[0]]


def dilate(mask, size):
    """Nới mặt nạ bool một ô vuông size×size (như cv2.dilate với nhân vuông)."""
    from scipy import ndimage
    return ndimage.maximum_filter(mask.astype(np.uint8), size=size) > 0


def old_product_mask(body_mask, extra_masks, product_box, hand_mask):
    """SP cũ = mặt nạ thân + phần mặt nạ "cả vật" trong khung SP nối liền thân (giữ quai, bỏ đồ trên kệ phía sau)."""
    from scipy import ndimage
    h, w = body_mask.shape
    pad = 0.04 * max(product_box[2] - product_box[0], product_box[3] - product_box[1])
    in_box = np.zeros_like(body_mask)
    in_box[int(max(0, product_box[1] - pad)):int(min(h, product_box[3] + pad)),
           int(max(0, product_box[0] - pad)):int(min(w, product_box[2] + pad))] = True
    extra = np.zeros_like(body_mask)
    for m in extra_masks:
        extra |= m
    extra &= in_box & ~hand_mask
    labels, _ = ndimage.label(extra | body_mask, structure=np.ones((3, 3)))
    seed = dilate(body_mask, 11)
    keep = np.unique(labels[seed & (labels > 0)])
    return body_mask | np.isin(labels, keep[keep > 0])


def classify_hands(hand_boxes, old_body):
    """(tay nắm quai, tay đỡ đáy, tay ôm thân, có cầm dây đeo dài không) theo vị trí tâm tay so với thân SP cũ."""
    top, bottom = old_body[1], old_body[3]
    height = max(1.0, bottom - top)
    grips, supports, sides = [], [], []
    for hb in hand_boxes:
        cy = (hb[1] + hb[3]) / 2
        if cy < top + GRIP_BAND * height:
            grips.append(hb)
        elif cy > bottom - SUPPORT_BAND * height:
            supports.append(hb)
        else:
            sides.append(hb)
    long_strap = any(hb[1] + GRIP_IN_HAND * (hb[3] - hb[1]) < top - LONG_STRAP * height for hb in grips)
    return grips, supports, sides, long_strap


def skin_mask(rgb):
    """Điểm ảnh màu da theo luật YCbCr (SKIN_CB/SKIN_CR/SKIN_MIN_LUMA) — chỉ có nghĩa trong khung tay."""
    f = rgb.astype(np.float32)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return ((cb >= SKIN_CB[0]) & (cb <= SKIN_CB[1]) & (cr >= SKIN_CR[0]) & (cr <= SKIN_CR[1])
            & (luma >= SKIN_MIN_LUMA))


def _box_mask(shape, hb, pad=0.0):
    H, W = shape
    bw, bh = hb[2] - hb[0], hb[3] - hb[1]
    m = np.zeros(shape, bool)
    m[int(max(0, hb[1] - pad * bh)):int(min(H, np.ceil(hb[3] + pad * bh))),
      int(max(0, hb[0] - pad * bw)):int(min(W, np.ceil(hb[2] + pad * bw)))] = True
    return m


def hand_core(skin, hb):
    """Vùng da liền khối chạm khung tay nhiều nhất; rỗng nếu quá ít da (găng tay, ánh sáng lạ)."""
    from scipy import ndimage
    labels, n = ndimage.label(skin & _box_mask(skin.shape, hb, HAND_KEEP_PAD))
    if not n:
        return np.zeros_like(skin)
    overlap = ndimage.sum(_box_mask(skin.shape, hb), labels, np.arange(1, n + 1))
    if overlap.max() < MIN_SKIN_FRACTION * (hb[2] - hb[0]) * (hb[3] - hb[1]):
        return np.zeros_like(skin)
    return labels == (int(np.argmax(overlap)) + 1)


def segment_hand(sam, core, hb, negatives):
    """Mặt nạ một bàn tay: SAM nhắc bằng khung + điểm sâu nhất trong lõi da, điểm âm ở `negatives`. SAM ôm đúng lõi
    thì lấy SAM quanh lõi, không thì lấy lõi; không có lõi thì dùng SAM."""
    from scipy import ndimage
    region = _box_mask(core.shape, hb, HAND_KEEP_PAD)
    bw, bh = hb[2] - hb[0], hb[3] - hb[1]
    H, W = core.shape
    pw, ph = HAND_PROMPT_PAD * bw, HAND_PROMPT_PAD * bh
    prompt = [[max(0.0, hb[0] - pw), max(0.0, hb[1] - ph)], [min(W - 1.0, hb[2] + pw), min(H - 1.0, hb[3] + ph)]]
    negatives = [list(p) for p in negatives]
    if not core.any():
        return sam.mask(negatives + prompt, [0] * len(negatives) + [2, 3]) & region
    depth = ndimage.distance_transform_edt(core)
    y1, x1 = np.unravel_index(np.argmax(depth), depth.shape)
    yy, xx = np.indices(depth.shape)
    far = depth * ((yy - y1) ** 2 + (xx - x1) ** 2 > (0.3 * max(bw, bh)) ** 2)
    positives = [[x1, y1]]
    if far.max() > 0:
        y2, x2 = np.unravel_index(np.argmax(far), far.shape)
        positives.append([x2, y2])
    m = sam.mask(positives + negatives + prompt, [1] * len(positives) + [0] * len(negatives) + [2, 3]) & region
    deep = depth >= 0.5 * depth.max()
    good = (m & deep).sum() >= SAM_CORE_COVER * deep.sum() and m.sum() <= SAM_MAX_GROWTH * core.sum()
    hand = (m & dilate(core, 2 * HAND_EDGE_PX + 1)) if good else core
    # Lấp lỗ (móng, chỗ bóng) để SP mới không lộ qua thành đốm trên ngón.
    hand = ndimage.binary_closing(hand, structure=np.ones((3, 3)), iterations=HAND_EDGE_PX)
    return ndimage.binary_fill_holes(hand) & region


def whole_product_mask(sam, bag, body, grip_hands, handle_pts):
    """SAM "cả SP cũ": khung SP + điểm dương ở thân/quai cũ + điểm âm giữa nắm tay."""
    points = [[(body[0] + body[2]) / 2, (body[1] + body[3]) / 2]] + [list(p) for p in handle_pts]
    labels = [1] * len(points)
    for hb in grip_hands:
        points.append([(hb[0] + hb[2]) / 2, hb[1] + 0.3 * (hb[3] - hb[1])])
        labels.append(0)
    return sam.mask(points + [[bag[0], bag[1]], [bag[2], bag[3]]], labels + [2, 3])


def handle_points(handle, core, hb):
    """≤ 2 điểm trên quai cũ ngay dưới nắm tay — điểm âm khi tách bàn tay."""
    bh = hb[3] - hb[1]
    window = np.zeros_like(handle)
    window[int(hb[1] + 0.6 * bh):int(hb[3] + 0.3 * bh), int(hb[0]):int(np.ceil(hb[2]))] = True
    cand = handle & ~core & window
    points = []
    mid = (hb[0] + hb[2]) / 2
    for side in (lambda x: x < mid, lambda x: x >= mid):
        ys, xs = np.nonzero(cand)
        pick = side(xs)
        if pick.sum() >= 20:
            points.append([float(xs[pick].mean()), float(ys[pick].mean())])
    return points


def handle_by_color(body_mask, rgb, zone, skin_core, grip_boxes):
    """Mặt nạ quai SP cũ trong `zone` (dưới nắm tay tới đỉnh thân): gần màu thân hơn màu da và nối ra ngoài khung tay.
    Màu túi trùng màu nền quanh tay thì chỉ nhận vệt mảnh."""
    from scipy import ndimage
    core = ndimage.binary_erosion(body_mask, iterations=5)
    depth = ndimage.distance_transform_edt(skin_core)
    skin = depth >= 0.5 * depth.max() if depth.max() > 0 else skin_core
    ys, xs = np.nonzero(zone)
    if core.sum() < 50 or skin.sum() < 20 or not len(ys):
        return np.zeros_like(zone)
    pixels = rgb[core].astype(np.float32)
    ref = np.median(pixels, axis=0)
    spread = max(6.0, float(np.median(np.linalg.norm(pixels - ref, axis=1))))
    # 3 màu da chuẩn theo độ sáng; bỏ khỏi mẫu các điểm sát màu túi (dây da lọt vào luật màu da).
    skin_px = rgb[skin].astype(np.float32)
    unlike_bag = np.linalg.norm(skin_px - ref, axis=1) > SKIN_SAMPLE_MIN_BAG_DIST * spread
    if unlike_bag.sum() >= 20:
        skin_px = skin_px[unlike_bag]
    order = np.argsort(skin_px.sum(axis=1))
    skin_refs = [np.median(part, axis=0) for part in np.array_split(skin_px[order], 3) if len(part)]
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub = rgb[y0:y1, x0:x1].astype(np.float32)
    to_bag = np.linalg.norm(sub - ref, axis=2)
    to_skin = np.min([np.linalg.norm(sub - r, axis=2) for r in skin_refs], axis=0)
    cand = zone[y0:y1, x0:x1] & (to_bag <= HANDLE_COLOR_TOL * spread) & (to_bag < to_skin)
    cand = ndimage.binary_opening(cand, iterations=1)  # bỏ chấm lẻ
    zone_sub = zone[y0:y1, x0:x1]
    if cand.sum() > HANDLE_AMBIGUOUS_FRACTION * zone_sub.sum():
        bx = np.flatnonzero(body_mask.any(axis=0))
        radius = max(2, int(HANDLE_MAX_WIDTH * (bx[-1] - bx[0] + 1) / 2)) if len(bx) else 2
        thick = ndimage.binary_opening(cand, structure=np.ones((3, 3)), iterations=radius)
        cand = cand & ~ndimage.binary_dilation(thick, iterations=2)
    thin = cand
    labels, _ = ndimage.label(thin, structure=np.ones((3, 3)))
    outside = np.unique(labels[thin & ~grip_boxes[y0:y1, x0:x1]])
    handle_sub = np.isin(labels, outside[outside > 0])
    handle = np.zeros_like(zone)
    handle[y0:y1, x0:x1] = handle_sub
    # Nới mép quai, không nới vào da.
    return handle | (dilate(handle, 5) & zone & ~skin_core)


def pixels_per_cm(grips, supports):
    """Điểm ảnh / cm theo cỡ bàn tay — None nếu không thấy tay."""
    for boxes, cm in ((grips, GRIP_HAND_CM), (supports, SUPPORT_HAND_CM)):
        if boxes:
            hb = boxes[0]
            return max(hb[2] - hb[0], hb[3] - hb[1]) / cm
    return None


def place_product(product_rows, apex_x, product_body, old_body, grips, supports, long_strap=False, width_cm=None,
                  px_per_cm=None):
    """→ (tỉ lệ phóng, x, y góc trên-trái ảnh SP). Có `width_cm` thì thân mới rộng đúng cỡ thật theo `px_per_cm` (hoặc
    thước bàn tay); không thì bằng thân cũ. Tay nắm: đỉnh quai vào nắm tay; tay đỡ: đáy tựa lên ngón; còn lại: theo tâm
    thân cũ."""
    ptop, pbot = product_rows
    px0, py0, px1, py1 = product_body
    ow, oh = old_body[2] - old_body[0], old_body[3] - old_body[1]
    cx, cy = (old_body[0] + old_body[2]) / 2, (old_body[1] + old_body[3]) / 2
    base = float(np.sqrt(ow * oh / max(1.0, (px1 - px0) * (py1 - py0))))
    per_cm = (px_per_cm or pixels_per_cm(grips, supports)) if width_cm else None
    real_size = per_cm is not None
    if real_size:
        base = float(width_cm * per_cm / max(1.0, px1 - px0))
    span = max(1.0, pbot - ptop)
    floor = min((hb[1] + SUPPORT_OVERLAP * (hb[3] - hb[1]) for hb in supports), default=None)
    if grips and not long_strap:
        hand = min(grips, key=lambda hb: hb[1])
        grip = hand[1] + GRIP_IN_HAND * (hand[3] - hand[1])
        scale = base
        if floor is not None and floor > grip and not real_size:
            scale = float(np.clip((floor - grip) / span, base * SCALE_MIN, base * SCALE_MAX))
        oy = max(grip - ptop * scale, hand[1] + GRIP_BODY_CLEAR * (hand[3] - hand[1]) - py0 * scale)
        body_ox = cx - (px0 + px1) / 2 * scale
        hand_ox = (hand[0] + hand[2]) / 2 - apex_x * scale
        ox = float(np.clip(hand_ox, body_ox - GRIP_SHIFT_MAX * ow, body_ox + GRIP_SHIFT_MAX * ow))
        return scale, ox, oy
    scale = base
    oy = cy - (py0 + py1) / 2 * scale
    if floor is not None:
        oy = floor - pbot * scale
    return scale, cx - (px0 + px1) / 2 * scale, oy


def product_apex_x(alpha):
    """Cột giữa đỉnh quai SP (điểm ảnh trong 4% chiều cao trên cùng) — chỗ tay nắm vào."""
    rows = np.flatnonzero((alpha > 128).any(axis=1))
    if not len(rows):
        return alpha.shape[1] / 2
    band = alpha[rows[0]:rows[0] + max(1, int(0.04 * (rows[-1] - rows[0] + 1)))] > 128
    cols = np.flatnonzero(band.any(axis=0))
    return float((cols[0] + cols[-1]) / 2)


def scene_warmth(rgb):
    """Độ ấm của nền 0..1 — như FE realism.estimateWarmth."""
    r, g, b = rgb.reshape(-1, 3).astype(np.float64).mean(axis=0)
    return float(np.clip(((r - b) / max(1.0, r + g + b)) * 3 / WARM_INDEX_FULL, 0, 1))


def harmonize(product_rgba, warmth):
    out = product_rgba.astype(np.float32)
    out[..., :3] *= np.array([1 + WARM_GAINS[0] * warmth, 1 + WARM_GAINS[1] * warmth, 1 + WARM_GAINS[2] * warmth], np.float32)
    return np.clip(out, 0, 255)


def inpaint(lama, rgb, hole, hide=None):
    """LaMa vẽ lại vùng vuông quanh lỗ, dán phần lỗ (mép mềm). `hide` (bàn tay) được che khỏi ngữ cảnh để LaMa không
    loang màu da."""
    from scipy import ndimage
    ys, xs = np.nonzero(hole)
    if not len(ys):
        return rgb
    h, w = hole.shape
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    side = min(max(h, w), int(max(y1 - y0, x1 - x0) * 1.6) + 16)
    cy, cx = (y0 + y1) // 2, (x0 + x1) // 2
    sy0, sx0 = max(0, min(h - side, cy - side // 2)), max(0, min(w - side, cx - side // 2))
    sy1, sx1 = min(h, sy0 + side), min(w, sx0 + side)
    crop = rgb[sy0:sy1, sx0:sx1]
    m = hole[sy0:sy1, sx0:sx1].astype(np.uint8)
    context = crop
    if hide is not None and hide[sy0:sy1, sx0:sx1].any():
        hidden = hide[sy0:sy1, sx0:sx1] & ~hole[sy0:sy1, sx0:sx1]
        _, (iy, ix) = ndimage.distance_transform_edt(hidden | hole[sy0:sy1, sx0:sx1], return_indices=True)
        context = crop.copy()
        context[hidden] = crop[iy[hidden], ix[hidden]]
    img = np.asarray(Image.fromarray(context).resize((LAMA_SIZE, LAMA_SIZE), Image.BOX), np.float32) / 255
    msk = (np.asarray(Image.fromarray(m * 255).resize((LAMA_SIZE, LAMA_SIZE), Image.NEAREST)) > 0).astype(np.float32)
    out = lama.run(None, {'image': img.transpose(2, 0, 1)[None], 'mask': msk[None, None]})[0][0].transpose(1, 2, 0)
    if out.max() <= 1.5:
        out = out * 255
    out = np.asarray(Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).resize((sx1 - sx0, sy1 - sy0), Image.BICUBIC), np.float32)
    soft = ndimage.gaussian_filter(m.astype(np.float32), 2)[..., None]
    res = rgb.copy()
    res[sy0:sy1, sx0:sx1] = (out * soft + crop * (1 - soft)).astype(np.uint8)
    return add_matched_grain(res, hole, rgb)


def refine_grip_fill(lama, clean, hole, hand_mask, grips):
    """Vẽ lại riêng vùng lỗ quanh tay nắm (lộ ra trong lòng quai túi mới) với khung nhỏ cho nét hơn."""
    for hb in grips:
        zone = _box_mask(hole.shape, hb, GRIP_REFILL_PAD) & hole
        if zone.sum() > 64:
            clean = inpaint(lama, clean, zone, hide=hand_mask)
    return clean


def front_layer(scene, alpha, product_body, scale, ox, oy):
    """Phần bàn tay vẽ đè lên SP mới: mặt nạ tay trước (front_mask) bỏ chỗ lòng bàn tay nắm quai rơi vào thân SP mới."""
    if not scene.grips:
        return scene.front_mask
    shape = scene.front_mask.shape
    body = np.zeros(shape, bool)
    bx0, by0, bx1, by1 = (ox + product_body[0] * scale, oy + product_body[1] * scale,
                          ox + product_body[2] * scale, oy + product_body[3] * scale)
    body[int(max(0, by0)):int(min(shape[0], np.ceil(by1))), int(max(0, bx0)):int(min(shape[1], np.ceil(bx1)))] = True
    product = alpha > 0.5
    body &= product
    zone = np.any([_box_mask(shape, hb, GRIP_LAYER_PAD) for hb in scene.grips], axis=0)
    # Chỉ ngón tay vẽ đè lên quai; lòng bàn tay / cổ tay nằm sau quai.
    fingers = np.zeros(shape, bool)
    for hb in scene.grips:
        fingers[int(max(0, hb[1])):int(min(shape[0], hb[1] + FINGERTIP_LINE * (hb[3] - hb[1]))),
                int(max(0, hb[0])):int(min(shape[1], np.ceil(hb[2])))] = True
    above_body = np.zeros(shape, bool)
    above_body[:int(max(0, by0)), :] = True
    return scene.front_mask & ~(zone & body) & ~(product & ~fingers & above_body)


def add_matched_grain(image, region, reference):
    """Thêm hạt vào `region` cho bằng độ chi tiết của `reference` ở dải quanh vùng đó."""
    from scipy import ndimage
    if not region.any():
        return image
    ys, xs = np.nonzero(region)
    pad = GRAIN_RING_GAP + GRAIN_RING_PX + 4
    y0, y1 = max(0, ys.min() - pad), min(region.shape[0], ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(region.shape[1], xs.max() + pad + 1)
    reg = region[y0:y1, x0:x1]
    ring = ndimage.binary_dilation(reg, iterations=GRAIN_RING_GAP + GRAIN_RING_PX) & \
        ~ndimage.binary_dilation(reg, iterations=GRAIN_RING_GAP)
    if ring.sum() < 100:
        return image

    def highpass(a):
        a = a.astype(np.float32)
        return a - ndimage.gaussian_filter(a, (GRAIN_HIGHPASS_SIGMA, GRAIN_HIGHPASS_SIGMA, 0))

    crop = image[y0:y1, x0:x1]
    # Ước lượng bền (MAD) — std bị các cạnh rõ kéo lên.
    robust = lambda v: 1.4826 * np.median(np.abs(v - np.median(v, axis=0)), axis=0)
    real = robust(highpass(reference[y0:y1, x0:x1])[ring])
    have = robust(highpass(crop)[reg])
    need = np.where(have < GRAIN_ENOUGH * real, np.sqrt(np.maximum(real ** 2 - have ** 2, 0)), 0)
    if not need.any():
        return image
    noise = ndimage.gaussian_filter(np.random.default_rng(GRAIN_SEED).standard_normal(reg.shape).astype(np.float32), GRAIN_SIGMA)
    noise /= max(1e-6, float((noise - ndimage.gaussian_filter(noise, GRAIN_HIGHPASS_SIGMA)).std()))
    soft = ndimage.gaussian_filter(reg.astype(np.float32), 2)
    out = image.copy()
    out[y0:y1, x0:x1] = np.clip(crop + noise[..., None] * need[None, None, :] * soft[..., None], 0, 255).astype(np.uint8)
    return out


class Scene:
    """Ảnh chị Nhạm đã xoá SP cũ. `clean` = None khi cầm dây dài (xoá sau khi đặt SP mới)."""

    def __init__(self, rgb, clean, hole, old_mask, hand_mask, front_mask, bag, body, hands, grips, supports, long_strap,
                 found=None, detector=''):
        self.rgb, self.clean, self.hole, self.old_mask = rgb, clean, hole, old_mask
        self.hand_mask, self.front_mask = hand_mask, front_mask
        self.bag, self.body, self.hands = bag, body, hands
        self.grips, self.supports, self.long_strap = grips, supports, long_strap
        self.found, self.detector = found, detector


def prepare_scene(models, person_image, debug=None):
    """Tìm SP cũ + bàn tay, tách tay, xoá SP cũ (LaMa) — mọi bước không phụ thuộc SP mới."""
    person_image = person_image.convert('RGB')
    if max(person_image.size) > MAX_SWAP_SIDE_PX:
        person_image.thumbnail((MAX_SWAP_SIDE_PX, MAX_SWAP_SIDE_PX), Image.LANCZOS)
    rgb = np.asarray(person_image)
    found = detect(models['person'], person_image)
    if not found.get('bag') or not found.get('bag_body'):
        raise ProductImageInputError('Không tìm thấy sản phẩm chị Nhạm đang cầm trong ảnh — chọn ảnh thấy rõ sản phẩm cũ.')
    bag, body = found['bag'][0][:4], found['bag_body'][0][:4]
    hands = [hb[:4] for hb in found.get('hand', [])][:2]
    sam = Sam(models, rgb)
    body_mask = sam.mask([[bag[0], bag[1]], [bag[2], bag[3]]], [2, 3])
    cx, cy = (body[0] + body[2]) / 2, (body[1] + body[3]) / 2
    grips, supports, _sides, long_strap = classify_hands(hands, body)
    hand_mask = np.zeros(body_mask.shape, bool)
    skin = skin_mask(rgb)
    # Tay nắm / ôm thân vẽ đè lên SP mới; tay đỡ đáy nằm dưới SP.
    front_mask = np.zeros(body_mask.shape, bool)
    cores = [hand_core(skin, hb) for hb in hands]
    handle = np.zeros(body_mask.shape, bool)
    if grips and not long_strap:
        zone = np.zeros(body_mask.shape, bool)
        for hb in grips:
            # Dải dưới nắm tay tới đỉnh thân — quai cũ đi qua đây.
            pad = HANDLE_ZONE_PAD * (hb[2] - hb[0])
            zone[int(hb[1]):int(body[1] + 0.05 * (body[3] - body[1])),
                 int(max(bag[0], hb[0] - pad)):int(min(bag[2], hb[2] + pad))] = True
        grip_boxes = np.any([_box_mask(zone.shape, hb) for hb in grips], axis=0)
        handle = handle_by_color(body_mask, rgb, zone, np.any(cores, axis=0) if cores else np.zeros_like(zone), grip_boxes)
        cores = [hand_core(skin & ~handle, hb) for hb in hands]
    grip_gaps = np.zeros(body_mask.shape, bool)
    handle_pts = []
    for hb, core in zip(hands, cores):
        pts = handle_points(handle, core, hb) if hb in grips else []
        handle_pts += pts
        negatives = [(cx, cy)] + pts
        m = segment_hand(sam, core, hb, negatives)
        hand_mask |= m
        if hb not in supports:
            front_mask |= m
        if hb in grips:
            box = np.zeros_like(m)
            box[int(hb[1] + GRIP_GAP_FROM * (hb[3] - hb[1])):int(np.ceil(hb[3])), int(hb[0]):int(np.ceil(hb[2]))] = True
            grip_gaps |= box & ~m & dilate(m, 2 * GRIP_GAP_PX + 1)
    handle &= ~hand_mask
    whole = whole_product_mask(sam, bag, body, [] if long_strap else grips, handle_pts)
    old_mask = old_product_mask(body_mask, sam.masks_box(bag), bag, hand_mask) | handle | (whole & ~hand_mask)
    if debug is not None:
        debug['handle'] = handle
    # Lỗ vẽ lại: SP cũ + cả khung thân, nới ra, trừ tay.
    hole = old_mask.copy()
    hole[int(body[1]):int(np.ceil(body[3])), int(body[0]):int(np.ceil(body[2]))] = True
    hole = (dilate(hole, 13) | grip_gaps) & ~hand_mask
    clean = None if long_strap else inpaint(models['lama'], rgb, hole, hide=hand_mask)
    if clean is not None:
        clean = refine_grip_fill(models['lama'], clean, hole, hand_mask, grips)
    if debug is not None:
        debug.update(found=found, hand_mask=hand_mask, front_mask=front_mask, old_mask=old_mask, hole=hole, clean=clean)
    return Scene(rgb, clean, hole, old_mask, hand_mask, front_mask, bag, body, hands, grips, supports, long_strap,
                 found, models['person'].name)


def trim_to_held_product(product_rgba, product_body):
    """Bỏ chân giá dưới đáy thân và cột giá mảnh trong lòng quai — còn dính sau tách nền."""
    from scipy import ndimage
    out = product_rgba.copy()
    x0, y0, x1, y1 = (float(v) for v in product_body)
    bh, bw = max(1.0, y1 - y0), max(1.0, x1 - x0)
    cut = int(min(out.shape[0], np.ceil(y1 + TRIM_BELOW_BODY * bh)))
    out[cut:, :, 3] = 0
    # Cột giá: cụm cột hẹp giữa thân, chạy liền từ đỉnh thân lên.
    top = int(max(0, np.flatnonzero((out[..., 3] > 128).any(axis=1))[0])) if (out[..., 3] > 128).any() else 0
    yb = int(max(0, y0))
    if yb - top > 8:
        zone = out[top:yb, :, 3] > 128
        cx = (x0 + x1) / 2
        lo, hi = int(max(0, cx - STAND_ROD_BAND * bw)), int(min(out.shape[1], cx + STAND_ROD_BAND * bw))
        run = np.zeros(out.shape[1], int)
        for c in range(lo, hi):
            col = zone[::-1, c]
            run[c] = int(np.argmin(col)) if not col.all() else len(col)
        tall = run >= STAND_ROD_MIN_RUN * (yb - top)
        labels, count = ndimage.label(tall)
        for i in range(1, count + 1):
            cols = np.flatnonzero(labels == i)
            if len(cols) <= STAND_ROD_MAX_WIDTH * bw:
                c0, c1 = cols[0] - 2, cols[-1] + 3
                rows = slice(yb - int(run[cols].max()), yb)
                out[rows, max(0, c0):c1, 3] = 0
    return out


def product_geometry(product_rgba):
    """(hàng đầu, hàng cuối) có điểm ảnh SP + cột giữa đỉnh quai — đầu vào place_product."""
    alpha = product_rgba[..., 3]
    rows = np.flatnonzero((alpha > 128).any(axis=1))
    product_rows = (int(rows[0]), int(rows[-1])) if len(rows) else (0, product_rgba.shape[0] - 1)
    return product_rows, product_apex_x(alpha)


def compose_product(models, scene, product_rgba, product_body, debug=None, width_cm=None, px_per_cm=None):
    """Đặt SP mới vào tay trong `scene` → (JPEG bytes, thông tin). `width_cm`: bề ngang thật; `px_per_cm`: thước ảnh."""
    from scipy import ndimage
    rgb, body, hand_mask, front_mask = scene.rgb, scene.body, scene.hand_mask, scene.front_mask
    product_rows, apex_x = product_geometry(product_rgba)
    scale, ox_f, oy_f = place_product(product_rows, apex_x, product_body, body, scene.grips, scene.supports,
                                      scene.long_strap, width_cm, px_per_cm)

    prod = np.asarray(Image.fromarray(harmonize(product_rgba, scene_warmth(rgb)).astype(np.uint8), 'RGBA').resize(
        (max(1, int(product_rgba.shape[1] * scale)), max(1, int(product_rgba.shape[0] * scale))), Image.LANCZOS), np.float32)
    ox, oy = int(round(ox_f)), int(round(oy_f))
    H, W = hand_mask.shape
    alpha = np.zeros((H, W), np.float32)
    color = np.zeros((H, W, 3), np.float32)
    x0, y0, x1, y1 = max(0, ox), max(0, oy), min(W, ox + prod.shape[1]), min(H, oy + prod.shape[0])
    if x1 > x0 and y1 > y0:
        p = prod[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
        alpha[y0:y1, x0:x1] = p[..., 3] / 255
        color[y0:y1, x0:x1] = p[..., :3]
    placed = np.flatnonzero((alpha > 0.5).any(axis=1))
    new_top = int(placed[0]) if len(placed) else H
    strap_kept = scene.long_strap
    clean = scene.clean
    if clean is None:
        # Cầm dây dài: giữ đoạn dây cũ phía trên SP mới.
        yy = np.arange(H)[:, None]
        strap = scene.old_mask & (yy < new_top + 0.03 * (body[3] - body[1]))
        strap &= ~dilate(alpha > 0.5, 5)
        strap[int(body[1]):, :] = False
        clean = inpaint(models['lama'], rgb, scene.hole & ~strap, hide=hand_mask)
        if debug is not None:
            debug['clean'] = clean

    bh = max(1.0, body[3] - body[1])
    shadow = ndimage.gaussian_filter(np.roll(alpha, int(0.02 * bh), axis=0), 0.03 * bh)
    out = clean.astype(np.float32) * (1 - SHADOW_OPACITY * shadow * (1 - alpha))[..., None]
    front = front_layer(scene, alpha, product_body, scale, ox, oy)
    # Bóng tiếp xúc chỉ quanh phần tay ở trước túi.
    dist = ndimage.distance_transform_edt(~front)
    color *= (1 - CONTACT_DARKEN * np.clip(1 - dist / (0.025 * bh), 0, 1))[..., None]
    out = color * alpha[..., None] + out * (1 - alpha[..., None])
    ha = ndimage.gaussian_filter(front.astype(np.float32), 1.2)[..., None]
    out = rgb * ha + out * (1 - ha)
    buffer = io.BytesIO()
    Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(buffer, 'JPEG', quality=JPEG_QUALITY)
    warnings = []
    if not scene.hands:
        warnings.append('Không thấy bàn tay cầm SP — ảnh có thể trông như SP lơ lửng, xem lại trước khi dùng.')
    return buffer.getvalue(), {
        'width': W, 'height': H, 'hands': len(scene.hands), 'strap_kept': bool(strap_kept), 'scale': round(scale, 3),
        'detector': scene.detector, 'warnings': warnings,
        'real_size': bool(width_cm and (px_per_cm or pixels_per_cm(scene.grips, scene.supports))),
    }


def swap_product(models, person_image, product_rgba, product_body, debug=None, width_cm=None):
    """Đổi SP trên một ảnh chị Nhạm → (JPEG bytes, thông tin). `debug` (dict) nhận các mặt nạ trung gian."""
    return compose_product(models, prepare_scene(models, person_image, debug), product_rgba, product_body, debug, width_cm)
