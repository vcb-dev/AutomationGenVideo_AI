"""Thư viện tư thế chị Nhạm cầm SP — chế độ tự chọn tư thế, 0đ.

Mỗi ảnh chị Nhạm được xoá SP cũ sẵn một lần (`manage.py build_pose_library`). Khi có túi mới: chấm điểm mọi tư thế
(phần túi cũ không được che, lệch tay đỡ, lệch quai, tràn ảnh, lệch cỡ cm, khác dáng) rồi ghép vào các tư thế tốt nhất.
Thước cm = khung ảnh (FRAME_HEIGHT_CM).

Biến môi trường bắt buộc: POSE_LIBRARY_DIR_ENV (thư mục thư viện), POSE_CANDIDATES_ENV (số ảnh trả về mỗi lượt)."""
import json
import os
import threading

import numpy as np
from PIL import Image

from video_management.services import held_swap as hs
from video_management.services.product_image_service import ProductImageConfigError, ProductImageInputError

POSE_LIBRARY_DIR_ENV = 'PRODUCT_IMAGE_POSE_LIBRARY_DIR'
POSE_CANDIDATES_ENV = 'PRODUCT_IMAGE_POSE_CANDIDATES'
INDEX_FILE = 'index.json'
# Tư thế loại sau khi soát bằng mắt: {id: lý do} — không bắt buộc có.
EXCLUDED_FILE = 'excluded.json'
RGB_FILE, CLEAN_FILE, MASKS_FILE, META_FILE = 'rgb.jpg', 'clean.jpg', 'masks.npz', 'meta.json'
JPEG_QUALITY = 93
SCORE_LONG_SIDE = 384
SUPPORT_GAP_WEIGHT = 0.5
HANDLE_MISMATCH_WEIGHT = 0.3
OUTSIDE_WEIGHT = 1.0
SIZE_MISMATCH_WEIGHT = 1.0
SIZE_GATE = 0.3  # |ln(cỡ mới / cỡ cũ)| quá mốc này → xếp sau tư thế cùng cỡ
# Ngưỡng loại tư thế (usable_fit / load_library).
MAX_UNCOVERED = 0.3
MIN_SHAPE_IOU = 0.6
SHAPE_WEIGHT = 1.0
MAX_SIZE_MISMATCH = 0.45
MIN_GRIP_COVERAGE = 0.6  # mặt nạ tay nắm phủ ít màu da = tách tay hỏng
FRAME_HEIGHT_CM = 96.0  # chiều cao thật của khung ảnh thư viện
SIMILAR_POSE_IOU = 0.7  # bỏ tư thế gần trùng tư thế đã chọn

_library = {}
_library_lock = threading.Lock()


def read_library_settings():
    """(thư mục thư viện, số ảnh trả về) — kiểm tồn tại/hợp lệ, thiếu thì báo đúng tên biến."""
    folder = (os.environ.get(POSE_LIBRARY_DIR_ENV) or '').strip()
    if not folder:
        raise ProductImageConfigError(
            f'{POSE_LIBRARY_DIR_ENV} chưa được set trên AI Service (thư mục thư viện tư thế chị Nhạm cầm SP, dựng bằng '
            f'`manage.py build_pose_library`).'
        )
    if not os.path.isfile(os.path.join(folder, INDEX_FILE)):
        raise ProductImageConfigError(f'{POSE_LIBRARY_DIR_ENV}={folder} không có {INDEX_FILE} — chưa dựng thư viện tư thế.')
    raw = (os.environ.get(POSE_CANDIDATES_ENV) or '').strip()
    if not raw:
        raise ProductImageConfigError(f'{POSE_CANDIDATES_ENV} chưa được set trên AI Service (số ảnh ghép trả về mỗi lượt).')
    try:
        count = int(raw)
    except ValueError:
        count = 0
    if count < 1:
        raise ProductImageConfigError(f'{POSE_CANDIDATES_ENV}={raw} phải là số nguyên ≥ 1.')
    return folder, count


def pose_usable(scene):
    """→ (dùng được không, lý do): cần tay nắm quai hoặc tay đỡ đáy, không cầm dây dài."""
    if scene.long_strap:
        return False, 'cầm dây đeo dài'
    if not scene.hands:
        return False, 'không thấy bàn tay'
    if not scene.grips and not scene.supports:
        return False, 'không có tay nắm quai / tay đỡ đáy'
    return True, ''


def grip_coverage(rgb, front_mask, grips):
    """Tỉ lệ điểm màu da trong khung tay nắm quai được mặt nạ tay trước phủ — None nếu không có tay nắm."""
    if not grips:
        return None
    x0, y0, x1, y1 = (int(round(v)) for v in grips[0])
    skin = hs.skin_mask(rgb[y0:y1, x0:x1])
    if not skin.any():
        return None
    return round(float((front_mask[y0:y1, x0:x1] & skin).sum()) / float(skin.sum()), 3)


def save_pose(scene, folder, pose_id, source):
    """Ghi một tư thế đã dựng (ảnh gốc thu nhỏ, ảnh đã xoá SP cũ, mặt nạ, khung) vào folder/pose_id."""
    out = os.path.join(folder, pose_id)
    os.makedirs(out, exist_ok=True)
    Image.fromarray(scene.rgb).save(os.path.join(out, RGB_FILE), 'JPEG', quality=JPEG_QUALITY)
    Image.fromarray(scene.clean).save(os.path.join(out, CLEAN_FILE), 'JPEG', quality=JPEG_QUALITY)
    shape = scene.rgb.shape[:2]
    np.savez_compressed(
        os.path.join(out, MASKS_FILE),
        **{name: np.packbits(getattr(scene, name)) for name in ('hand_mask', 'front_mask', 'old_mask')},
        shape=np.array(shape),
    )
    meta = {
        'id': pose_id, 'source': source, 'width': shape[1], 'height': shape[0],
        'bag': [float(v) for v in scene.bag], 'body': [float(v) for v in scene.body],
        'hands': [[float(v) for v in hb] for hb in scene.hands],
        'grips': [[float(v) for v in hb] for hb in scene.grips],
        'supports': [[float(v) for v in hb] for hb in scene.supports],
        'detector': scene.detector,
        'grip_coverage': grip_coverage(scene.rgb, scene.front_mask, scene.grips),
    }
    with open(os.path.join(out, META_FILE), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)
    return meta


def write_index(folder, metas):
    with open(os.path.join(folder, INDEX_FILE), 'w', encoding='utf-8') as f:
        json.dump({'poses': [m['id'] for m in metas]}, f, ensure_ascii=False)


def _downscale(mask, size):
    return np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize(size, Image.BILINEAR)) > 127


class Pose:
    """Một tư thế trong thư viện: meta + mặt nạ thu nhỏ để chấm điểm; ảnh/mặt nạ đủ cỡ chỉ nạp khi ghép thật."""

    def __init__(self, folder, meta):
        self.folder, self.meta, self.id = folder, meta, meta['id']
        masks = np.load(os.path.join(folder, self.id, MASKS_FILE))
        h, w = (int(v) for v in masks['shape'])
        self.factor = SCORE_LONG_SIDE / max(h, w)
        small = (max(1, round(w * self.factor)), max(1, round(h * self.factor)))
        full = {k: np.unpackbits(masks[k])[:h * w].reshape(h, w).astype(bool) for k in ('old_mask', 'hand_mask')}
        self.old_small = _downscale(full['old_mask'], small)
        self.hand_small = _downscale(full['hand_mask'], small)

    def scene(self):
        """held_swap.Scene đủ cỡ để compose_product ghép thật."""
        base = os.path.join(self.folder, self.id)
        masks = np.load(os.path.join(base, MASKS_FILE))
        h, w = (int(v) for v in masks['shape'])
        unpack = {k: np.unpackbits(masks[k])[:h * w].reshape(h, w).astype(bool)
                  for k in ('hand_mask', 'front_mask', 'old_mask')}
        rgb = np.asarray(Image.open(os.path.join(base, RGB_FILE)).convert('RGB'))
        clean = np.asarray(Image.open(os.path.join(base, CLEAN_FILE)).convert('RGB'))
        m = self.meta
        return hs.Scene(rgb, clean, None, unpack['old_mask'], unpack['hand_mask'], unpack['front_mask'],
                        tuple(m['bag']), tuple(m['body']), [tuple(hb) for hb in m['hands']],
                        [tuple(hb) for hb in m['grips']], [tuple(hb) for hb in m['supports']], False,
                        detector=m.get('detector', ''))


def load_library(folder):
    """Các tư thế dùng được — nạp lại khi INDEX_FILE đổi."""
    index_path = os.path.join(folder, INDEX_FILE)
    stamp = os.path.getmtime(index_path)
    with _library_lock:
        cached = _library.get(folder)
        if cached and cached[0] == stamp:
            return cached[1]
        with open(index_path, encoding='utf-8') as f:
            ids = json.load(f)['poses']
        excluded_path = os.path.join(folder, EXCLUDED_FILE)
        if os.path.isfile(excluded_path):
            with open(excluded_path, encoding='utf-8') as f:
                excluded = set(json.load(f))
            ids = [pose_id for pose_id in ids if pose_id not in excluded]
        poses = []
        for pose_id in ids:
            with open(os.path.join(folder, pose_id, META_FILE), encoding='utf-8') as f:
                meta = json.load(f)
            coverage = meta.get('grip_coverage')
            if coverage is not None and coverage < MIN_GRIP_COVERAGE:
                continue
            poses.append(Pose(folder, meta))
        if not poses:
            raise ProductImageConfigError(f'{POSE_LIBRARY_DIR_ENV}={folder}: thư viện tư thế rỗng.')
        _library[folder] = (stamp, poses)
        return poses


def handle_ratio(top, body):
    """Chiều cao quai (từ đỉnh SP tới đỉnh thân) / chiều cao thân — 0 = không quai (ví, clutch)."""
    return max(0.0, body[1] - top) / max(1.0, body[3] - body[1])


def pose_pixels_per_cm(meta):
    """Điểm ảnh / cm của ảnh thư viện theo khung ảnh (FRAME_HEIGHT_CM)."""
    return meta['height'] / FRAME_HEIGHT_CM


def old_bag_cm(meta):
    """Bề ngang thân túi chị Nhạm đang cầm trong ảnh gốc (cm)."""
    return (meta['body'][2] - meta['body'][0]) / pose_pixels_per_cm(meta)


def size_mismatch(meta, width_cm):
    """|ln(cỡ túi mới / cỡ túi cũ)|; 0 nếu không biết cỡ túi mới."""
    if not width_cm:
        return 0.0
    return abs(float(np.log(width_cm / max(1.0, old_bag_cm(meta)))))


def score_pose(pose, product_alpha, product_rows, apex_x, product_body, width_cm=None):
    """Điểm hợp (càng thấp càng hợp) khi đặt SP mới vào tư thế này — xem chú thích đầu file."""
    m = pose.meta
    body, grips, supports = m['body'], m['grips'], m['supports']
    scale, ox, oy = hs.place_product(product_rows, apex_x, product_body, body, grips, supports, False, width_cm,
                                     pose_pixels_per_cm(m))
    f = pose.factor
    sh, sw = pose.old_small.shape
    pw, ph = product_alpha.shape[1], product_alpha.shape[0]
    tw, th = max(1, round(pw * scale * f)), max(1, round(ph * scale * f))
    placed_small = np.asarray(Image.fromarray(product_alpha).resize((tw, th), Image.BILINEAR)) > 127
    x, y = round(ox * f), round(oy * f)
    alpha = np.zeros((sh, sw), bool)
    x0, y0, x1, y1 = max(0, x), max(0, y), min(sw, x + tw), min(sh, y + th)
    inside = 0
    if x1 > x0 and y1 > y0:
        crop = placed_small[y0 - y:y1 - y, x0 - x:x1 - x]
        alpha[y0:y1, x0:x1] = crop
        inside = int(crop.sum())
    total = max(1, int(placed_small.sum()))
    old = pose.old_small & ~pose.hand_small
    uncovered = float((old & ~alpha).sum()) / max(1, int(old.sum()))
    visible_new = alpha & ~pose.hand_small
    shape_iou = float((old & visible_new).sum()) / max(1, int((old | visible_new).sum()))
    height = max(1.0, (product_rows[1] - product_rows[0]) * scale)
    gap = 0.0
    if supports:
        floor = min(hb[1] + hs.SUPPORT_OVERLAP * (hb[3] - hb[1]) for hb in supports)
        gap = abs(oy + product_rows[1] * scale - floor) / height
    mismatch = abs(handle_ratio(product_rows[0], product_body) - handle_ratio(m['bag'][1], body))
    outside = 1.0 - inside / total
    size_gap = size_mismatch(m, width_cm)
    score = uncovered + SUPPORT_GAP_WEIGHT * min(gap, 1.0) + HANDLE_MISMATCH_WEIGHT * min(mismatch, 1.0) \
        + OUTSIDE_WEIGHT * outside + SIZE_MISMATCH_WEIGHT * min(size_gap, 1.0) + SHAPE_WEIGHT * (1 - shape_iou)
    return score, {'uncovered': round(uncovered, 3), 'support_gap': round(gap, 3),
                   'handle_mismatch': round(mismatch, 3), 'outside': round(outside, 3), 'size_mismatch': round(size_gap, 3),
                   'shape_iou': round(shape_iou, 3)}


def rank_poses(poses, product_rgba, product_body, width_cm=None):
    """[(điểm, tư thế, chi tiết điểm)] tăng dần — tư thế hợp nhất trước."""
    rows, apex_x = hs.product_geometry(product_rgba)
    alpha = np.ascontiguousarray(product_rgba[..., 3])
    ranked = []
    for pose in poses:
        score, detail = score_pose(pose, alpha, rows, apex_x, product_body, width_cm)
        ranked.append((score, pose, detail))
    # Tư thế cùng cỡ trước, lệch cỡ quá SIZE_GATE sau.
    ranked.sort(key=lambda item: (item[2].get('size_mismatch', 0) > SIZE_GATE,
                                  item[2].get('size_mismatch', 0) if item[2].get('size_mismatch', 0) > SIZE_GATE else 0,
                                  item[0]))
    return ranked


def _relative_box(meta, key):
    w, h = meta['width'], meta['height']
    b = meta[key]
    return b[0] / w, b[1] / h, b[2] / w, b[3] / h


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def pick_distinct(ranked, count):
    """`count` tư thế tốt nhất, ưu tiên không trùng nhau."""
    chosen = []
    for item in ranked:
        box = _relative_box(item[1].meta, 'body')
        if all(_iou(box, _relative_box(c[1].meta, 'body')) < SIMILAR_POSE_IOU for c in chosen):
            chosen.append(item)
        if len(chosen) == count:
            return chosen
    for item in ranked:
        if len(chosen) == count:
            break
        if item not in chosen:
            chosen.append(item)
    return chosen


def usable_fit(detail):
    """Tư thế đủ khớp để ghép cho thật: túi mới che gần hết túi cũ, không tràn khỏi ảnh, cùng dáng, không lệch cỡ quá xa."""
    return (detail['uncovered'] <= MAX_UNCOVERED and detail['outside'] <= MAX_UNCOVERED
            and detail['shape_iou'] >= MIN_SHAPE_IOU and detail.get('size_mismatch', 0) <= MAX_SIZE_MISMATCH)


def compose_best(models, poses, product_rgba, product_body, count, width_cm=None):
    """Ghép SP mới vào `count` tư thế hợp nhất → [(JPEG bytes, thông tin)]."""
    if not poses:
        raise ProductImageInputError('Thư viện tư thế rỗng.')
    product_rgba = hs.trim_to_held_product(product_rgba, product_body)
    all_ranked = rank_poses(poses, product_rgba, product_body, width_cm)
    ranked = [item for item in all_ranked if usable_fit(item[2])]
    if not ranked and width_cm and all_ranked and all(
            item[2].get('size_mismatch', 0) > MAX_SIZE_MISMATCH for item in all_ranked
            if item[2]['shape_iou'] >= MIN_SHAPE_IOU):
        sizes = sorted(round(old_bag_cm(p.meta)) for p in poses)
        raise ProductImageInputError(
            f'Chưa có ảnh chị Nhạm cầm túi cỡ {width_cm:g}cm cùng dáng này (thư viện có túi {sizes[0]}–{sizes[-1]}cm). '
            f'Kiểm tra lại số cm — bề ngang THÂN túi thật.'
        )
    if not ranked:
        raise ProductImageInputError(
            'Chưa có ảnh chị Nhạm cầm túi cùng dáng, cùng cỡ để ghép cho thật (túi này khác dáng mọi túi trong thư viện tư '
            'thế — ghép vào tay sẽ lệch, lộ vết xoá túi cũ). Cần media chụp thêm chị Nhạm cầm túi dáng này.'
        )
    results = []
    for score, pose, detail in pick_distinct(ranked, count):
        image_bytes, info = hs.compose_product(models, pose.scene(), product_rgba, product_body, width_cm=width_cm,
                                               px_per_cm=pose_pixels_per_cm(pose.meta))
        info.update(pose_id=pose.id, pose_source=pose.meta.get('source'), score=round(score, 3), score_detail=detail)
        results.append((image_bytes, info))
    return results
