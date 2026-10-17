"""Làm sạch ảnh tách nền rembg trên ảnh SP phông trơn — 0đ, numpy/scipy.

1. Nội suy màu phông phía sau SP; phông không trơn thì để nguyên.
2. Xoá mảng phông rembg giữ nhầm (lòng quai, giữa dây và thân) nằm ngoài thân túi.
3. Mép: tính lại độ đặc theo màu, khử ám phông; dây mảnh khác màu phông thì làm đặc.
Đầu ra kiểu rembg (màu đã nhân độ đặc).
"""
import numpy as np

KNOWN_BACKDROP_MAX_ALPHA = 16
# Nội suy phông.
BACKDROP_SIGMA_FRACTION = 0.05
BACKDROP_MIN_WEIGHT = 1e-4
PLAIN_BACKDROP_MAX_NOISE = 4.0
# Trùng màu phông (ΔE).
BACKDROP_MATCH_MIN_DE = 6.0
BACKDROP_MATCH_NOISE_FACTOR = 3.0
BACKDROP_MATCH_MAX_DE = 12.0
# Điều kiện một mảng là phông giữ nhầm.
LEFTOVER_MIN_FRACTION = 0.002
LEFTOVER_MIN_PX = 24
LEFTOVER_MAX_FRACTION = 0.6
# Gọt quai/dây (mở hình thái) để còn thân túi.
BODY_CORE_RADIUS_OF_BODY = 0.12
BODY_CORE_RADIUS_OF_PRODUCT = 0.06
INSIDE_BODY_MAX_OVERLAP = 0.3  # mảng nằm trong thân quá tỉ lệ này là chi tiết trên thân → giữ
LEFTOVER_MIN_RADIUS_FRACTION = 0.015  # đủ rộng — vệt sáng mảnh trên da bóng không bị xoá
LEFTOVER_MIN_RADIUS_PX = 4
LEFTOVER_REACH = 1.5  # mảng phông thật chạm phông ngoài SP qua quai/dây mảnh
SOFT_SHADOW_MIN_RATIO = 0.6  # bóng mờ cùng sắc phông cũng là phông
# Bóng tiếp xúc sát đáy túi mà rembg không chắc → xoá loang từ mảng phông.
CONTACT_SHADOW_MIN_RATIO = 0.2
CONTACT_SHADOW_MAX_CHROMA_SHIFT = 22.0
UNSURE_MAX_ALPHA = 245
LEFTOVER_OUTSIDE_FRACTION = 0.1
# SP cùng màu phông quá tỉ lệ này → không xoá gì.
SIMILAR_PRODUCT_MAX_FRACTION = 0.4
# Dải mép.
EDGE_BAND_FRACTION = 0.005
EDGE_BAND_MIN_PX = 2
FRINGE_FRACTION = 0.0015
FRINGE_MIN_PX = 1
UNMIX_MIN_CONTRAST = 25.0
# Điểm trộn: nằm trên đoạn phông→màu SP.
MIX_MIN_T = 0.05
MIX_MAX_T = 0.95
MIX_MAX_RESIDUAL_PX = 12.0
MIX_MAX_RESIDUAL_FRACTION = 0.25
# Làm đặc dây mảnh.
RAISE_MIN_ALPHA = 0.25
CLEAR_FOREGROUND_DE = 25.0
# Bóng đổ: cùng sắc phông, tối hơn.
SHADOW_MAX_CHROMA_SHIFT = 8.0
SHADOW_MIN_RATIO = 0.25
SHADOW_MAX_RATIO = 0.97
DECONTAMINATE_MIN_ALPHA = 0.15
# Bóng/sàn ảnh gốc còn sót xa thân túi → xoá (FE tự vẽ bóng).
STRAY_SHADOW_MAX_ALPHA = 0.5


def _srgb_to_lab(rgb):
    """sRGB 0–255 (H, W, 3) → CIELAB (D65)."""
    c = rgb.astype(np.float32) / 255
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ np.array([[0.4124, 0.2126, 0.0193], [0.3576, 0.7152, 0.1192], [0.1805, 0.0722, 0.9505]], np.float32)
    xyz /= np.array([0.95047, 1.0, 1.08883], np.float32)
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def _luminance(rgb):
    c = np.asarray(rgb, np.float32) / 255
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return lin @ np.array([0.2126, 0.7152, 0.0722], np.float32)


def _normalized_blur(values, weights, sigma):
    """Nội suy mềm Σ(v·w)/Σw sau Gaussian → (giá trị, trọng số). Tính trên lưới thu nhỏ cho nhanh."""
    from scipy import ndimage
    values = values.astype(np.float32)
    weights = weights.astype(np.float32)
    squeeze = values.ndim == 2
    if squeeze:
        values = values[..., None]
    height, width = weights.shape
    factor = max(1, int(sigma // 3))
    if factor > 1:
        h, w = -(-height // factor), -(-width // factor)
        pad = ((0, h * factor - height), (0, w * factor - width))
        wsum = np.pad(weights, pad).reshape(h, factor, w, factor).sum(axis=(1, 3))
        vsum = np.pad(values * weights[..., None], pad + ((0, 0),)).reshape(h, factor, w, factor, -1).sum(axis=(1, 3))
        sigma_small = sigma / factor
    else:
        wsum, vsum, sigma_small = weights, values * weights[..., None], sigma
    blurred_w = ndimage.gaussian_filter(wsum, sigma_small, mode='nearest')
    blurred_v = np.stack([ndimage.gaussian_filter(vsum[..., c], sigma_small, mode='nearest') for c in range(vsum.shape[-1])], -1)
    estimate = blurred_v / np.maximum(blurred_w, 1e-12)[..., None]
    weight = blurred_w / (factor * factor)
    if factor > 1:
        zoom = (height / estimate.shape[0], width / estimate.shape[1])
        estimate = np.stack([ndimage.zoom(estimate[..., c], zoom, order=1)[:height, :width] for c in range(estimate.shape[-1])], -1)
        weight = ndimage.zoom(weight, zoom, order=1)[:height, :width]
    return (estimate[..., 0] if squeeze else estimate), weight


def _disk(radius):
    r = max(1, int(round(radius)))
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def estimate_backdrop(rgb, rembg_alpha):
    """(phông sRGB, phông Lab, chỗ đoán được, độ lổn nhổn) — None nếu ảnh không lộ phông."""
    from scipy import ndimage
    height, width = rembg_alpha.shape
    background = rembg_alpha <= KNOWN_BACKDROP_MAX_ALPHA
    # Chỉ lấy phông nối ra tới mép ảnh.
    labels, _ = ndimage.label(background)
    border = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    known = np.isin(labels, border[border > 0])
    if known.sum() < 0.05 * rembg_alpha.size:
        return None
    sigma = BACKDROP_SIGMA_FRACTION * max(height, width)
    rgb_f = rgb.astype(np.float32)
    backdrop_rgb, weight = _normalized_blur(rgb_f, known.astype(np.float32), sigma)
    lab = _srgb_to_lab(rgb)
    backdrop_lab = _srgb_to_lab(np.clip(backdrop_rgb, 0, 255))
    residual = np.linalg.norm(lab[known] - backdrop_lab[known], axis=1)
    noise = float(np.median(residual) * 1.4826)
    return backdrop_rgb, backdrop_lab, weight >= BACKDROP_MIN_WEIGHT * weight.max(), noise


def _body_silhouette(material, radius):
    """Thân SP: gọt quai/dây mảnh hơn `radius`, lấp lỗ — tính trên lưới thu nhỏ."""
    from scipy import ndimage
    height, width = material.shape
    factor = max(1, int(radius // 4))
    if factor > 1:
        h, w = -(-height // factor), -(-width // factor)
        padded = np.pad(material, ((0, h * factor - height), (0, w * factor - width)))
        small = padded.reshape(h, factor, w, factor).mean(axis=(1, 3)) >= 0.5
    else:
        small = material
    body = ndimage.binary_fill_holes(ndimage.binary_opening(small, structure=_disk(radius / factor)))
    if factor > 1:
        body = np.repeat(np.repeat(body, factor, axis=0), factor, axis=1)[:height, :width]
    return body


def leftover_backdrop(lab, backdrop_lab, valid, alpha, threshold, body_box=None, outside=None, rgb=None, backdrop_rgb=None,
                      rembg_alpha=None):
    """(mặt nạ phông giữ nhầm, lý do bỏ qua | None). `outside`: phông ngoài SP."""
    from scipy import ndimage
    product = alpha >= 128
    area = int(product.sum())
    if area == 0:
        return np.zeros_like(product), 'không có SP'
    distance = np.linalg.norm(lab - backdrop_lab, axis=-1)
    like = distance < threshold
    if rgb is not None and backdrop_rgb is not None:
        chroma_shift = np.linalg.norm(lab[..., 1:] - backdrop_lab[..., 1:], axis=-1)
        ratio = _luminance(rgb) / np.maximum(_luminance(backdrop_rgb), 1e-4)
        like |= (chroma_shift < SHADOW_MAX_CHROMA_SHIFT) & (ratio >= SOFT_SHADOW_MIN_RATIO) & (ratio <= 1.0)
    like &= valid & product
    core = ndimage.binary_erosion(alpha >= 250, iterations=2)
    in_body = None
    if body_box is not None:
        in_body = np.zeros_like(core)
        in_body[int(body_box[1]):int(np.ceil(body_box[3])), int(body_box[0]):int(np.ceil(body_box[2]))] = True
        core &= in_body
    if core.any() and float((like & core).sum()) / core.sum() > SIMILAR_PRODUCT_MAX_FRACTION:
        return np.zeros_like(product), 'SP gần cùng màu phông'
    ys, xs = np.nonzero(product)
    span = max(ys.max() - ys.min(), xs.max() - xs.min()) + 1
    if body_box is not None and min(body_box[2] - body_box[0], body_box[3] - body_box[1]) > 0:
        radius = BODY_CORE_RADIUS_OF_BODY * min(body_box[2] - body_box[0], body_box[3] - body_box[1])
    else:
        radius = BODY_CORE_RADIUS_OF_PRODUCT * span
    body = _body_silhouette(product & ~like, radius)
    if body_box is not None:
        body = body | in_body
    labels, count = ndimage.label(like, structure=np.ones((3, 3)))
    if not count:
        return np.zeros_like(product), None
    index = np.arange(1, count + 1)
    sizes = ndimage.sum(like, labels, index)
    inside = ndimage.sum(body, labels, index)
    radius = ndimage.maximum(ndimage.distance_transform_edt(like), labels, index)
    keep = (sizes >= max(LEFTOVER_MIN_PX, LEFTOVER_MIN_FRACTION * area)) & (sizes <= LEFTOVER_MAX_FRACTION * area) \
        & (inside <= INSIDE_BODY_MAX_OVERLAP * sizes) \
        & (radius >= max(LEFTOVER_MIN_RADIUS_PX, LEFTOVER_MIN_RADIUS_FRACTION * span))
    if outside is not None:
        objects = ndimage.find_objects(labels)
        for i in np.flatnonzero(keep):
            keep[i] = _reaches_outside(labels, i + 1, objects[i], radius[i] * LEFTOVER_REACH, outside)
    removed = np.isin(labels, index[keep])
    removed = ndimage.binary_fill_holes(removed) & ~body & product
    if removed.any() and rgb is not None and backdrop_rgb is not None:
        unsure = (rembg_alpha if rembg_alpha is not None else alpha) < UNSURE_MAX_ALPHA
        contact = unsure & (chroma_shift < CONTACT_SHADOW_MAX_CHROMA_SHIFT) \
            & (ratio >= CONTACT_SHADOW_MIN_RATIO) & (ratio <= 1.0) & valid & product
        removed = ndimage.binary_propagation(removed, structure=np.ones((3, 3)), mask=removed | contact)
    return removed, None


def _reaches_outside(labels, label, box, reach, outside):
    """Vành nới `reach` px quanh mảng `label` có ≥ LEFTOVER_OUTSIDE_FRACTION điểm là phông ngoài SP không."""
    from scipy import ndimage
    height, width = labels.shape
    pad = int(np.ceil(reach)) + 1
    y0, y1 = max(0, box[0].start - pad), min(height, box[0].stop + pad)
    x0, x1 = max(0, box[1].start - pad), min(width, box[1].stop + pad)
    region = labels[y0:y1, x0:x1] == label
    ring = ~region & (ndimage.distance_transform_edt(~region) <= reach)
    return ring.any() and float(outside[y0:y1, x0:x1][ring].mean()) >= LEFTOVER_OUTSIDE_FRACTION


def refine_edges(rgb, alpha, backdrop_rgb, backdrop_lab, lab, valid, removed, threshold):
    """(độ đặc 0–1, màu đã khử ám phông) cho dải mép."""
    from scipy import ndimage
    height, width = alpha.shape
    a = alpha.astype(np.float32) / 255
    band_px = max(EDGE_BAND_MIN_PX, int(round(EDGE_BAND_FRACTION * max(height, width))))
    outside = (a <= KNOWN_BACKDROP_MAX_ALPHA / 255) | removed
    near_edge = ndimage.binary_dilation(outside, structure=_disk(band_px))
    band = near_edge & (a > 0) & ~removed & valid
    solid = (a >= 250 / 255) & ~band
    rgb_f = rgb.astype(np.float32)
    foreground, weight = _normalized_blur(rgb_f, solid.astype(np.float32), 2 * band_px)
    has_fg = weight >= 0.05
    B = backdrop_rgb
    d = foreground - B
    contrast = np.linalg.norm(d, axis=-1)
    t = np.sum((rgb_f - B) * d, axis=-1) / np.maximum(contrast ** 2, 1e-6)
    off_line = np.linalg.norm(rgb_f - (B + t[..., None] * d), axis=-1)
    on_line = off_line <= np.maximum(MIX_MAX_RESIDUAL_PX, MIX_MAX_RESIDUAL_FRACTION * contrast)
    new_a = a.copy()
    fringe_px = max(FRINGE_MIN_PX, int(round(FRINGE_FRACTION * max(height, width))))
    fringe = ndimage.binary_dilation(outside, structure=_disk(fringe_px)) | (a < 250 / 255)
    usable = band & fringe & has_fg & (contrast >= UNMIX_MIN_CONTRAST) & on_line
    # Điểm trộn → độ đặc = vị trí trên đoạn (chỉ làm trong hơn).
    mixed = usable & (t >= MIX_MIN_T) & (t <= MIX_MAX_T)
    backdrop_like = usable & (t < MIX_MIN_T) & (a < 250 / 255)
    new_a[mixed] = np.minimum(a[mixed], t[mixed])
    new_a[backdrop_like] = 0
    distance = np.linalg.norm(lab - backdrop_lab, axis=-1)
    chroma_shift = np.linalg.norm(lab[..., 1:] - backdrop_lab[..., 1:], axis=-1)
    ratio = _luminance(rgb_f) / np.maximum(_luminance(B), 1e-4)
    shadow = (chroma_shift < SHADOW_MAX_CHROMA_SHIFT) & (ratio >= SHADOW_MIN_RATIO) & (ratio <= SHADOW_MAX_RATIO)
    thin = band & ~has_fg & (a >= RAISE_MIN_ALPHA) & (a < 250 / 255) & (distance >= CLEAR_FOREGROUND_DE) & ~shadow
    new_a[thin] = 1.0
    new_a[band & shadow] = np.minimum(new_a[band & shadow], a[band & shadow])
    stray = valid & ~has_fg & (a < 250 / 255) & ((distance < threshold) | (shadow & (a < STRAY_SHADOW_MAX_ALPHA)))
    new_a[stray] = 0
    new_a[removed] = 0
    color = rgb_f.copy()
    edge = mixed & (new_a > 0)
    divisor = np.maximum(new_a, DECONTAMINATE_MIN_ALPHA)[..., None]
    pure = np.clip(B + (rgb_f - B) / divisor, 0, 255)
    color[edge] = pure[edge]
    faint = edge & (new_a < DECONTAMINATE_MIN_ALPHA) & has_fg
    color[faint] = foreground[faint]
    return new_a, color


def refine_cutout(rgb, alpha, rembg_alpha, body_box=None):
    """→ (RGBA uint8 kiểu rembg, {'applied', 'reason', 'leftover_removed'})."""
    rgb = np.asarray(rgb, np.uint8)
    alpha = np.asarray(alpha, np.uint8)
    info = {'applied': False, 'reason': None, 'leftover_removed': 0.0}
    estimate = estimate_backdrop(rgb, np.asarray(rembg_alpha, np.uint8))
    if estimate is None:
        info['reason'] = 'ảnh không lộ phông'
        return _premultiply(rgb, alpha.astype(np.float32) / 255), info
    backdrop_rgb, backdrop_lab, valid, noise = estimate
    if noise > PLAIN_BACKDROP_MAX_NOISE:
        info['reason'] = f'phông không trơn (lổn nhổn {noise:.1f})'
        return _premultiply(rgb, alpha.astype(np.float32) / 255), info
    lab = _srgb_to_lab(rgb)
    threshold = float(np.clip(BACKDROP_MATCH_NOISE_FACTOR * noise, BACKDROP_MATCH_MIN_DE, BACKDROP_MATCH_MAX_DE))
    outside = np.asarray(rembg_alpha, np.uint8) <= KNOWN_BACKDROP_MAX_ALPHA
    removed, skipped = leftover_backdrop(lab, backdrop_lab, valid, alpha, threshold, body_box, outside, rgb, backdrop_rgb,
                                         np.asarray(rembg_alpha, np.uint8))
    new_a, color = refine_edges(rgb, alpha, backdrop_rgb, backdrop_lab, lab, valid, removed, threshold)
    product_area = max(1, int((alpha >= 128).sum()))
    info.update(applied=True, reason=skipped, leftover_removed=round(float(removed.sum()) / product_area, 4))
    return _premultiply(color, new_a), info


def _premultiply(color, a):
    """Màu (float/uint8) + độ đặc 0–1 → RGBA uint8, màu điểm nửa trong suốt đã nhân độ đặc như rembg."""
    a = np.clip(a, 0, 1)
    rgb = np.asarray(color, np.float32) * np.where(a < 1, a, 1)[..., None]
    out = np.dstack([np.clip(np.round(rgb), 0, 255), np.round(a * 255)]).astype(np.uint8)
    out[a == 0] = 0
    return out
