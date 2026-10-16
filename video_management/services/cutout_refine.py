"""
Làm sạch ảnh tách nền của rembg trên ảnh SP chụp PHÔNG TRƠN (ảnh NCC/catalog) — 0đ, chạy CPU, chỉ numpy/scipy
(có sẵn theo rembg). Người dùng soát 15/10 thấy ảnh ghép "giả": rembg giữ nhầm phông trong lòng quai, giữa dây đeo
và thân túi; dây mảnh rembg để nửa trong suốt thành "bóng ma"; mép còn ám màu phông.

1. Ước lượng phông phía sau SP: nội suy mềm (Gaussian chuẩn hoá) từ chính các điểm rembg coi là phông và nối ra
   tới mép ảnh — chịu được phông chuyển màu, bục trắng dưới túi. Phông lổn nhổn (ảnh chụp phố, ảnh shop có kệ)
   → không đụng gì, trả nguyên ảnh rembg.
2. Xoá mảng phông bị giữ nhầm: vùng trong mặt nạ trùng màu phông (so phông nội suy tại đúng chỗ đó), đủ lớn, và
   nằm NGOÀI thân túi. Thân túi = phần không trùng màu phông sau khi gọt bỏ quai/dây mảnh (mở hình thái), lấp lỗ —
   nên logo/miếng da trắng/lớp lót nằm trên thân được giữ. SP gần cùng màu phông (túi trắng trên phông trắng) thì
   không xoá gì.
3. Mép: điểm mép là trộn màu SP với màu phông → tính lại độ đặc theo màu (chiếu lên đoạn phông→màu SP gần đó),
   khử ám màu phông (viền sáng quanh túi tối). Dây mảnh không có điểm đặc gần đó mà màu khác hẳn phông (không phải
   bóng đổ) → làm đặc.

Đầu ra theo đúng kiểu rembg (màu điểm nửa trong suốt ĐÃ NHÂN độ đặc) — FE `unpremultiplyEdges` chia lại.
"""
import numpy as np

# rembg coi điểm có alpha ≤ mốc này là phông.
KNOWN_BACKDROP_MAX_ALPHA = 16
# Độ rộng Gaussian khi nội suy phông vào chỗ SP che (theo cạnh dài ảnh) — phông studio đổi màu rất chậm; lỗ quai
# rộng ~1/4 túi vẫn lấy được phông ngay ngoài quai.
BACKDROP_SIGMA_FRACTION = 0.05
# Trọng số nội suy dưới mốc này = quá xa mọi điểm phông → không đoán phông ở đó.
BACKDROP_MIN_WEIGHT = 1e-4
# Độ lổn nhổn của phông (ΔE CIELAB, trung vị lệch ×1,48) quá mốc này → không phải phông trơn → không đụng gì.
PLAIN_BACKDROP_MAX_NOISE = 4.0
# Trùng màu phông: lệch ΔE < max(MIN, k·độ lổn nhổn), không quá MAX.
BACKDROP_MATCH_MIN_DE = 6.0
BACKDROP_MATCH_NOISE_FACTOR = 3.0
BACKDROP_MATCH_MAX_DE = 12.0
# Mảng phông giữ nhầm: ít nhất chừng này phần diện tích SP (và ≥ MIN_PX), nhiều nhất MAX (lớn hơn là chính SP).
LEFTOVER_MIN_FRACTION = 0.002
LEFTOVER_MIN_PX = 24
LEFTOVER_MAX_FRACTION = 0.6  # sàn dưới túi bị dây đeo bao quanh có thể bằng nửa SP (ảnh Pexels 15/10); đã có thêm luật
# "ngoài thân túi" + "chạm phông ngoài" nên không xoá nhầm thân
# Gọt quai/dây để còn thân túi: bán kính mở hình thái = tỉ lệ này × cạnh NGẮN của khung thân (model nhận diện trả).
# Quai/dây dày ~3–6% bề ngang túi — gọt theo 3% cỡ SP thì quai dày vẫn còn, lòng quai bị lấp vào "thân" và phông
# trong đó không được xoá (thử 15/10); thân túi dày hơn nhiều nên 12% cạnh ngắn không gọt mất thân.
BODY_CORE_RADIUS_OF_BODY = 0.12
# Không có khung thân (model không thấy thân): theo cạnh dài của cả SP.
BODY_CORE_RADIUS_OF_PRODUCT = 0.06
# Mảng trùng màu phông mà nằm trong thân túi quá tỉ lệ này → là chi tiết trên thân (logo, lót) → giữ.
INSIDE_BODY_MAX_OVERLAP = 0.3
# Mảng phông thật (lòng quai, vòng dây) đủ RỘNG: bán kính hình tròn lớn nhất lọt trong mảng ≥ tỉ lệ này cạnh dài SP.
# Vệt sáng dài mảnh trên quai da bóng (phản chiếu phông xám — soát 15/10: quai bị xoá thành rách) hẹp hơn nhiều.
LEFTOVER_MIN_RADIUS_FRACTION = 0.015
LEFTOVER_MIN_RADIUS_PX = 4
# Mảng phông thật nằm sau quai/dây MẢNH: nới mảng ra REACH × bán kính của nó là chạm phông ngoài SP (rembg alpha ≈ 0)
# ở ít nhất OUTSIDE_FRACTION vành nới. Vệt sáng lớn trên da bóng (miếng da đính quai phản chiếu phông — soát 15/10 bị
# xoá thành lỗ) nằm giữa da, vành nới vẫn là da.
LEFTOVER_REACH = 1.5
# Phông có bóng mờ của ảnh gốc (sàn ngay dưới túi, khoảng giữa dây đeo nằm trên sàn và đáy túi — soát 15/10 ảnh Pexels):
# cùng sắc phông, tối hơn một chút → cũng tính là phông khi tìm mảng giữ nhầm. Tối hơn mốc dưới là màu SP (túi xám).
SOFT_SHADOW_MIN_RATIO = 0.6
# Bóng tiếp xúc ngay sát đáy túi trong ảnh gốc (tối hơn, ngả sắc hơn — đo 15/10: tỉ lệ sáng 0,2–0,8, lệch a*b* tới ~20)
# mà rembg cũng KHÔNG chắc (alpha < UNSURE_MAX_ALPHA): xoá loang từ mảng phông đã xoá sang các điểm này nếu nối liền.
CONTACT_SHADOW_MIN_RATIO = 0.2
CONTACT_SHADOW_MAX_CHROMA_SHIFT = 22.0
UNSURE_MAX_ALPHA = 245
LEFTOVER_OUTSIDE_FRACTION = 0.1
# Quá tỉ lệ này phần lõi SP trùng màu phông → SP cùng màu phông (túi trắng trên phông trắng) → không xoá mảng nào.
SIMILAR_PRODUCT_MAX_FRACTION = 0.4
# Dải mép lấy màu SP gần đó để tách màu (theo cạnh dài ảnh, tối thiểu MIN_PX).
EDGE_BAND_FRACTION = 0.005
EDGE_BAND_MIN_PX = 2
# Chỉ những điểm sát rìa trong suốt chừng này (viền ám phông) hoặc rembg vốn để nửa trong suốt mới được tính lại độ
# đặc — điểm đặc nằm sâu hơn (vệt sáng giữa quai bóng) giữ nguyên.
FRINGE_FRACTION = 0.0015
FRINGE_MIN_PX = 1
# Màu SP và màu phông cách nhau ít hơn mốc này (sRGB) thì không tách được theo màu → giữ độ đặc của rembg.
UNMIX_MIN_CONTRAST = 25.0
# Điểm mép là TRỘN màu SP với màu phông: nằm trên đoạn phông→màu SP (vị trí t trong [MIN, MAX], lệch khỏi đoạn không quá
# max(PX, tỉ lệ × độ dài đoạn)). Điểm sáng phản chiếu trên da bóng (sáng hơn phông, hoặc lệch hẳn khỏi đoạn) không
# phải điểm trộn — soát 15/10: viền sáng quai bóng bị coi là phông, quai thành nét đứt.
MIX_MIN_T = 0.05
MIX_MAX_T = 0.95
MIX_MAX_RESIDUAL_PX = 12.0
MIX_MAX_RESIDUAL_FRACTION = 0.25
# Chỉ làm đặc điểm rembg đã cho ≥ mốc này (đã nghĩ là SP), màu cách phông ≥ CLEAR_DE và không giống bóng đổ.
RAISE_MIN_ALPHA = 0.25
CLEAR_FOREGROUND_DE = 25.0
# Bóng đổ trên phông: cùng sắc phông (lệch a*b* < CHROMA) và tối đi trong khoảng [MIN, MAX] lần độ chói phông.
SHADOW_MAX_CHROMA_SHIFT = 8.0
SHADOW_MIN_RATIO = 0.25
SHADOW_MAX_RATIO = 0.97
# Độ đặc dưới mốc này thì không chia màu (nhiễu bị khuếch đại) — lấy màu SP gần đó.
DECONTAMINATE_MIN_ALPHA = 0.15
# Bóng/sàn của ẢNH GỐC rembg giữ lại mờ mờ dưới đáy túi (xa thân túi, không có màu SP gần đó): trùng màu phông, hoặc
# giống bóng đổ mà độ đặc dưới mốc này → xoá. Để lại thì trên hộp gỗ thành tấm "kính mờ" sáng chữ nhật dưới đáy túi
# (soát 15/10); bóng thật dưới chân do FE vẽ theo số đo ảnh chụp thật.
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
    """Nội suy mềm: Σ(giá trị·trọng số) / Σ(trọng số) sau Gaussian — trả (giá trị, trọng số 0–1).
    Gaussian rộng thì tính trên lưới gộp `factor`×`factor` điểm rồi phóng lại (kết quả vốn mượt): ảnh 2048px với
    σ≈100px tính thẳng mất ~30s (đo 15/10), gộp lưới còn dưới 1s."""
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
    """(màu phông nội suy sRGB, Lab tương ứng, chỗ đoán được, độ lổn nhổn ΔE) — None nếu ảnh không lộ phông.
    Lấy phông theo mặt nạ GỐC của rembg (trước bước nhận diện): kệ/đạo cụ bước nhận diện xoá đi không phải phông."""
    from scipy import ndimage
    height, width = rembg_alpha.shape
    background = rembg_alpha <= KNOWN_BACKDROP_MAX_ALPHA
    # Chỉ lấy phông nối ra tới mép ảnh — mẩu SP rembg bỏ sót nằm giữa túi không bị coi là phông.
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
    """Thân SP: phần vật liệu SP còn lại sau khi gọt quai/dây mảnh hơn `radius` (mở hình thái), lấp lỗ. Tính trên lưới
    thu nhỏ để bán kính còn ~4 điểm — đĩa bán kính 36px trên ảnh 2048px mất ~10s (đo 15/10), thân chỉ cần đại khái."""
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
    """Mặt nạ các mảng phông rembg giữ nhầm (xem bước 2 đầu file) + lý do bỏ qua (None nếu đã xét).
    `body_box` [x0, y0, x1, y1] khung thân SP (model nhận diện) — để biết quai/dây dày cỡ nào thì gọt. `outside`: phông
    ngoài SP (rembg alpha ≈ 0) — mảng phông giữ nhầm phải chạm tới đó qua quai/dây mảnh."""
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
        # Xét màu SP trên đúng thân túi — mảng phông giữ nhầm (sàn dưới túi) không được kéo kết luận "SP cùng màu phông".
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
        # Khung thân của model nhận diện cũng là thân: vệt phản chiếu xám trên da bóng ở mép trên thân (nối ra ngoài qua
        # lòng quai) không được xoá (soát 15/10).
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
    # Lỗ trong mảng đã xoá (chỗ xa mọi điểm phông nên không đoán được màu phông) cũng là phông — trừ thân túi.
    removed = ndimage.binary_fill_holes(removed) & ~body & product
    if removed.any() and rgb is not None and backdrop_rgb is not None:
        # Độ chắc GỐC của rembg: khung thân model hơi dài hơn đáy túi thật nên solidify_body đã làm đặc cả dải bóng sát
        # đáy (soát 15/10 — dải sáng dưới đáy túi hồng).
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
    """(độ đặc 0–1, màu SP đã khử ám phông) cho dải mép — xem bước 3 đầu file."""
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
    # Điểm trộn thật → độ đặc = vị trí trên đoạn. Điểm trùng màu phông chỉ xoá khi chính rembg cũng chưa chắc (< 250):
    # rembg đã cho đặc thì đó thường là điểm sáng phản chiếu trên SP. Chỉ làm TRONG hơn, không làm đặc hơn.
    mixed = usable & (t >= MIX_MIN_T) & (t <= MIX_MAX_T)
    backdrop_like = usable & (t < MIX_MIN_T) & (a < 250 / 255)
    new_a[mixed] = np.minimum(a[mixed], t[mixed])
    new_a[backdrop_like] = 0
    # Dây mảnh: không có điểm đặc gần đó để biết màu SP — màu khác hẳn phông và không phải bóng đổ thì làm đặc.
    distance = np.linalg.norm(lab - backdrop_lab, axis=-1)
    chroma_shift = np.linalg.norm(lab[..., 1:] - backdrop_lab[..., 1:], axis=-1)
    ratio = _luminance(rgb_f) / np.maximum(_luminance(B), 1e-4)
    shadow = (chroma_shift < SHADOW_MAX_CHROMA_SHIFT) & (ratio >= SHADOW_MIN_RATIO) & (ratio <= SHADOW_MAX_RATIO)
    thin = band & ~has_fg & (a >= RAISE_MIN_ALPHA) & (a < 250 / 255) & (distance >= CLEAR_FOREGROUND_DE) & ~shadow
    new_a[thin] = 1.0
    # Bóng đổ: không được làm đặc hơn rembg (chỉ tách mép thì độ đặc theo màu có thể cao hơn).
    new_a[band & shadow] = np.minimum(new_a[band & shadow], a[band & shadow])
    # Bóng/sàn ảnh gốc còn sót xa thân túi → xoá (xem STRAY_SHADOW_MAX_ALPHA).
    stray = valid & ~has_fg & (a < 250 / 255) & ((distance < threshold) | (shadow & (a < STRAY_SHADOW_MAX_ALPHA)))
    new_a[stray] = 0
    new_a[removed] = 0
    color = rgb_f.copy()
    # Khử ám phông chỉ ở điểm trộn thật — điểm khác (điểm sáng phản chiếu nửa trong suốt) giữ màu gốc như rembg.
    edge = mixed & (new_a > 0)
    divisor = np.maximum(new_a, DECONTAMINATE_MIN_ALPHA)[..., None]
    pure = np.clip(B + (rgb_f - B) / divisor, 0, 255)
    color[edge] = pure[edge]
    faint = edge & (new_a < DECONTAMINATE_MIN_ALPHA) & has_fg
    color[faint] = foreground[faint]
    return new_a, color


def refine_cutout(rgb, alpha, rembg_alpha, body_box=None):
    """rgb (H, W, 3) uint8 ảnh gốc, alpha (H, W) uint8 sau bước nhận diện, rembg_alpha mặt nạ gốc của rembg, body_box
    khung thân SP của model nhận diện (cùng toạ độ, None nếu không có) →
    (RGBA uint8 kiểu rembg, thông tin {'applied', 'reason': lý do không làm | None, 'leftover_removed': tỉ lệ diện
    tích SP đã xoá})."""
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
