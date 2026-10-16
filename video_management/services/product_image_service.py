"""Tạo ảnh cho sản phẩm mới chưa về hàng — phần thuần logic (kiểm đầu vào, tách nền, prompt Gemini).

1. Tách nền (0đ): rembg + model nhận diện giữ đúng SP, bỏ giá/đạo cụ → PNG trong suốt; FE ghép lên nền.
2. Chị Nhạm cầm SP (Gemini, tính phí): thay SP trên ảnh chị Nhạm thật.
3. Đặt SP vào cảnh (Gemini, tính phí).
Vô trạng thái như id_photo_views.py.
"""
import base64
import binascii
import io
import os
import threading

import numpy as np
from PIL import Image, ImageOps

MAX_INPUT_IMAGE_MB = 12

ALLOWED_INPUT_MIME_TYPES = {'image/jpeg', 'image/jpg', 'image/png', 'image/webp'}

# Thu nhỏ ảnh SP trước khi tách nền — đủ để túi trên ảnh ghép 2560px không phải phóng.
MAX_CUTOUT_SIDE_PX = 3072

# Alpha dưới mốc này coi là nền khi cắt sát mép (quầng mờ rembg).
TRIM_ALPHA_THRESHOLD = 16

# Dải tách rời chiếm ít hơn tỉ lệ này tổng alpha của SP = vụn nền, không tính vào khung cắt.
TRIM_MIN_BAND_FRACTION = 0.01

# Cảnh báo ảnh gốc: SP chạm mép ảnh (bị cụt) hoặc quá nhỏ (ghép sẽ mờ).
CUT_OFF_ALPHA = 128
CUT_OFF_EDGE_FRACTION = 0.02
SHARP_PRODUCT_MIN_SIDE_PX = 700
_SIDE_LABELS = {'top': 'trên', 'bottom': 'dưới', 'left': 'trái', 'right': 'phải'}

HELD_PRODUCT_NOTE_MAX_CHARS = 500
# Bề ngang thật thân túi (cm) — khớp khoảng bên BE/FE.
WIDTH_CM_MIN = 6
WIDTH_CM_MAX = 60

# Biến môi trường bắt buộc, không mặc định.
REMBG_MODEL_ENV = 'REMBG_MODEL'
GEMINI_MODEL_ENV = 'GEMINI_MODEL'


class ProductImageInputError(Exception):
    """Đầu vào sai (thiếu ảnh, sai định dạng, ảnh không có SP) — view trả 400."""


class ProductImageConfigError(Exception):
    """AI service thiếu/sai cấu hình — view trả 500 kèm tên biến cần sửa."""


# ═══════════════════════════════════════════════════════════════
# Đầu vào dùng chung cho cả hai chế độ
# ═══════════════════════════════════════════════════════════════

def decode_input_image(image_base64, mime_type, field_label):
    """Giải mã ảnh base64 → (bytes, mime chuẩn hoá). `field_label` dùng trong thông báo lỗi."""
    if not image_base64:
        raise ProductImageInputError(f'Thiếu {field_label}.')
    mime = _check_input_mime(mime_type, field_label)
    try:
        data = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ProductImageInputError(f'{field_label.capitalize()} không giải mã được (base64 hỏng).')
    return check_input_image(data, mime, field_label)


def _check_input_mime(mime_type, field_label):
    mime = (mime_type or '').strip().lower()
    if not mime:
        raise ProductImageInputError(f'Thiếu mime_type của {field_label}.')
    if mime not in ALLOWED_INPUT_MIME_TYPES:
        raise ProductImageInputError(
            f'{field_label.capitalize()} phải là JPG, PNG hoặc WebP (nhận được: {mime}).'
        )
    return mime


def check_input_image(data, mime_type, field_label):
    """Kiểm 1 ảnh đã có bytes (file multipart BE gửi, hoặc base64 đã giải mã), trả (bytes, mime chuẩn hoá)."""
    mime = _check_input_mime(mime_type, field_label)
    if not data:
        raise ProductImageInputError(f'{field_label.capitalize()} rỗng.')
    size_mb = len(data) / (1024 * 1024)
    if size_mb > MAX_INPUT_IMAGE_MB:
        raise ProductImageInputError(
            f'{field_label.capitalize()} quá lớn ({size_mb:.1f}MB > {MAX_INPUT_IMAGE_MB}MB).'
        )
    # Gemini nhận image/jpeg, không nhận biến thể image/jpg.
    return data, ('image/jpeg' if mime == 'image/jpg' else mime)


def open_upright_image(image_bytes, field_label):
    """Mở ảnh và xoay theo EXIF."""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception:
        raise ProductImageInputError(f'{field_label.capitalize()} không phải file ảnh đọc được.')
    return ImageOps.exif_transpose(image)


# ═══════════════════════════════════════════════════════════════
# Chế độ 1 — tách nền ảnh SP bằng rembg
# ═══════════════════════════════════════════════════════════════

TRANSPARENT_MIN_ALPHA = 250


def transparency_of(image):
    """Kênh alpha (ảnh L) nếu ảnh có vùng trong suốt thật; None nếu ảnh đặc hoàn toàn / không có alpha."""
    if image.mode not in ('RGBA', 'LA', 'PA') and not (image.mode == 'P' and 'transparency' in image.info):
        return None
    alpha = image.convert('RGBA').getchannel('A')
    return alpha if alpha.getextrema()[0] < TRANSPARENT_MIN_ALPHA else None


def flatten_on_white(image):
    """Đặt ảnh có nền trong suốt lên nền trắng (bỏ màu rác dưới vùng trong suốt) → RGB."""
    rgba = image.convert('RGBA')
    white = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
    white.alpha_composite(rgba)
    return white.convert('RGB')

def read_rembg_model():
    model_name = (os.environ.get(REMBG_MODEL_ENV) or '').strip()
    if not model_name:
        raise ProductImageConfigError(
            f'{REMBG_MODEL_ENV} chưa được set trên AI Service (tên model tách nền của rembg, '
            f'vd isnet-general-use).'
        )
    return model_name


_rembg_sessions = {}
_rembg_sessions_lock = threading.Lock()


def get_rembg_session(model_name):
    """Session rembg theo tên model, nạp một lần mỗi worker."""
    with _rembg_sessions_lock:
        session = _rembg_sessions.get(model_name)
        if session is None:
            # Import muộn — rembg tốn ~500MB RAM mỗi worker.
            import onnxruntime as ort
            from rembg import new_session
            try:
                session = new_session(model_name, sess_opts=_low_memory_session_options(ort))
            except ValueError as e:
                raise ProductImageConfigError(
                    f'{REMBG_MODEL_ENV}={model_name} không phải model rembg hợp lệ: {e}'
                )
            _rembg_sessions[model_name] = session
        return session


def _low_memory_session_options(ort):
    """Tắt arena bộ nhớ của onnxruntime (~2,2GB → ~1GB mỗi worker, không chậm hơn)."""
    options = ort.SessionOptions()
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    return options


def downscale_to_max_side(image, max_side=MAX_CUTOUT_SIDE_PX):
    """Thu nhỏ giữ tỉ lệ nếu cạnh dài vượt `max_side`; ảnh nhỏ hơn giữ nguyên (không phóng to)."""
    width, height = image.size
    longest = max(width, height)
    if longest <= max_side:
        return image
    ratio = max_side / longest
    new_size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
    return image.resize(new_size, Image.LANCZOS)


def _main_span(mass, min_fraction):
    """Khoảng [đầu, cuối) phủ các dải liền có đủ khối lượng alpha trên một trục (bỏ vụn nền)."""
    total = float(mass.sum())
    bands = []
    start = None
    for i, value in enumerate(mass):
        if value and start is None:
            start = i
        elif not value and start is not None:
            bands.append((start, i))
            start = None
    if start is not None:
        bands.append((start, len(mass)))
    kept = [(s, e) for s, e in bands if mass[s:e].sum() >= total * min_fraction]
    return kept[0][0], kept[-1][1]


def trim_to_content(cutout, alpha_threshold=TRIM_ALPHA_THRESHOLD, min_band_fraction=TRIM_MIN_BAND_FRACTION):
    """Cắt ảnh RGBA sát mép SP, bỏ vụn nền tách rời."""
    rgba = cutout.convert('RGBA')
    return rgba.crop(content_box(rgba, alpha_threshold, min_band_fraction))


def content_box(rgba, alpha_threshold=TRIM_ALPHA_THRESHOLD, min_band_fraction=TRIM_MIN_BAND_FRACTION):
    """(left, top, right, bottom) phần SP trong ảnh RGBA — khung mà trim_to_content cắt ra."""
    alpha = np.asarray(rgba.getchannel('A'), dtype=np.int64)
    weights = np.where(alpha >= alpha_threshold, alpha, 0)
    if not weights.any():
        raise ProductImageInputError(
            'Không tìm thấy sản phẩm trong ảnh sau khi tách nền — thử ảnh SP rõ hơn, '
            'sản phẩm chiếm phần lớn khung hình.'
        )
    top, bottom = _main_span(weights.sum(axis=1), min_band_fraction)
    # Tìm cột trên đúng các hàng đã giữ, để vụn ở hàng bị bỏ không kéo rộng chiều ngang.
    left, right = _main_span(weights[top:bottom].sum(axis=0), min_band_fraction)
    return left, top, right, bottom


def cut_out_product(image_bytes, remove_background, locate_product=None, upscale=None, info=None):
    """Tách nền ảnh SP → (png_bytes, width, height, khung thân SP | None) đã cắt sát mép.

    `remove_background` (rembg), `locate_product` (nhận diện SP/đạo cụ/thân), `upscale` (phóng ảnh nhỏ) truyền vào để
    test không cần model thật. `info` (dict) nhận cỡ ảnh gốc, hệ số phóng, kết quả làm sạch mép, cảnh báo.
    """
    from video_management.services import cutout_refine

    image = open_upright_image(image_bytes, 'ảnh sản phẩm')
    # Ảnh đã trong suốt: đặt lên nền trắng (màu dưới vùng trong suốt là rác), rồi vẫn chạy rembg.
    source_alpha = transparency_of(image)
    image = flatten_on_white(image) if source_alpha is not None else image.convert('RGB')
    source_size = image.size
    factor = 1
    if upscale is not None:
        image, factor = upscale(image)
    image = downscale_to_max_side(image)
    cutout = remove_background(image).convert('RGBA')
    if source_alpha is not None:
        # Vùng gốc đã trong suốt chắc chắn là nền.
        source_alpha = np.asarray(source_alpha.resize(image.size, Image.BILINEAR))
        cutout.putalpha(Image.fromarray(np.minimum(np.asarray(cutout.getchannel('A')), source_alpha)))
    rembg_alpha = np.asarray(cutout.getchannel('A'))
    # None = không nhận diện được → giữ kết quả rembg.
    located = locate_product(image, np.asarray(cutout.getchannel('A'))) if locate_product is not None else None
    body_box = None
    if located is not None:
        # Phần tử thứ 4 (nếu có): mặt nạ quai/dây ngoài khung cần giữ.
        product_box, prop_boxes, body_box, *keep = located
        original = np.asarray(cutout.getchannel('A'))
        alpha = isolate_product(original, product_box, prop_boxes)
        if keep and keep[0] is not None:
            alpha = np.where(keep[0], original, alpha)
        if body_box is not None:
            solid = solidify_body(alpha, body_box)
            raised = solid > alpha
            if raised.any():
                # rembg trộn màu điểm nửa trong suốt với nền đen → lấy lại màu thật từ ảnh gốc.
                rgba = np.array(cutout)
                rgba[..., :3][raised] = np.asarray(image)[raised]
                cutout = Image.fromarray(rgba, 'RGBA')
            alpha = solid
        cutout.putalpha(Image.fromarray(alpha))
    if source_alpha is not None:
        cutout.putalpha(Image.fromarray(np.minimum(np.asarray(cutout.getchannel('A')), source_alpha)))
    # Làm sạch mép trên ảnh gốc.
    rgba, refine = cutout_refine.refine_cutout(np.asarray(image), np.asarray(cutout.getchannel('A')), rembg_alpha, body_box)
    if source_alpha is not None:
        rgba = rgba.copy()
        keep = np.minimum(rgba[..., 3], source_alpha)
        # Ảnh premultiplied → giảm màu theo alpha.
        scale = np.where(rgba[..., 3] > 0, keep / np.maximum(rgba[..., 3], 1), 0)[..., None]
        rgba[..., :3] = np.round(rgba[..., :3] * scale).astype(np.uint8)
        rgba[..., 3] = keep
    cutout = Image.fromarray(rgba, 'RGBA')
    left, top, right, bottom = content_box(cutout)
    if info is not None:
        info.update(
            source_size=list(source_size), upscaled=factor, refine=refine,
            cut_off_sides=cut_off_sides(rgba[..., 3]),
            product_source_px=round(max(right - left, bottom - top) * source_size[0] / image.width),
        )
    cutout = cutout.crop((left, top, right, bottom))
    buffer = io.BytesIO()
    cutout.save(buffer, format='PNG')
    return buffer.getvalue(), cutout.width, cutout.height, shift_box(body_box, left, top, cutout.width, cutout.height)


def parse_width_cm(raw):
    """Bề ngang thân SP (cm) → float; trống → None; sai → lỗi 400."""
    if raw is None or str(raw).strip() == '':
        return None
    try:
        value = float(str(raw).replace(',', '.'))
    except ValueError:
        raise ProductImageInputError(f'Bề ngang túi "{raw}" không phải số cm.')
    if not WIDTH_CM_MIN <= value <= WIDTH_CM_MAX:
        raise ProductImageInputError(f'Bề ngang túi {raw}cm ngoài khoảng {WIDTH_CM_MIN}–{WIDTH_CM_MAX}cm — kiểm tra lại số cm.')
    return value


def cut_off_sides(alpha, fraction=CUT_OFF_EDGE_FRACTION):
    """Các mép ảnh ('top', 'bottom', 'left', 'right') mà SP chạm vào — ảnh gốc cắt mất phần SP ở đó."""
    solid = np.asarray(alpha) >= CUT_OFF_ALPHA
    height, width = solid.shape
    edges = (('top', solid[0], width), ('bottom', solid[-1], width), ('left', solid[:, 0], height), ('right', solid[:, -1], height))
    return [name for name, line, length in edges if line.sum() > max(2, fraction * length)]


def input_quality_warnings(info):
    """Câu báo ảnh gốc không đủ làm ảnh thương mại (từ `info` của cut_out_product) — rỗng = ảnh gốc ổn."""
    warnings = []
    sides = info.get('cut_off_sides') or []
    if sides:
        where = ', '.join(_SIDE_LABELS[side] for side in sides)
        warnings.append(f'Ảnh gốc cắt mất một phần túi ở mép {where} — ảnh ghép sẽ thấy túi bị cụt; dùng ảnh thấy đủ cả túi.')
    product_px = info.get('product_source_px')
    if product_px and product_px < SHARP_PRODUCT_MIN_SIDE_PX:
        size = info.get('source_size')
        where = f'ảnh {size[0]}×{size[1]}px, ' if size else ''
        warnings.append(
            f'Ảnh gốc nhỏ ({where}túi chỉ ~{product_px}px) nên khi ghép phải phóng to nhiều lần — ảnh ra sẽ mờ, bệt. '
            f'Nếu lưu từ Google Hình ảnh: bấm vào ảnh, mở ảnh/trang gốc rồi mới lưu bản lớn (túi từ '
            f'{SHARP_PRODUCT_MIN_SIDE_PX}px trở lên).'
        )
    return warnings


def shift_box(box, left, top, width, height):
    """Khung theo toạ độ ảnh gốc → toạ độ ảnh đã cắt sát mép (kẹp trong ảnh); rỗng thì None."""
    if box is None:
        return None
    x0, y0, x1, y1 = (
        max(0, min(width, box[0] - left)), max(0, min(height, box[1] - top)),
        max(0, min(width, box[2] - left)), max(0, min(height, box[3] - top)),
    )
    return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None


# ─── Giữ đúng SP (rembg giữ cả giá trưng bày, đạo cụ) ─────────────────────

PRODUCT_BOX_MARGIN = 0.015
THIN_PROP_MAX_WIDTH = 0.12  # chỉ đạo cụ mảnh (thanh giá) mới bị xoá trong khung SP
BODY_ROW_COVERAGE = 0.6  # hàng phủ quá tỉ lệ này là hàng thân SP — không xoá
OPAQUE = 32

# Làm đặc thân túi sáng mà rembg để nửa trong suốt; lỗ to (khe quai) giữ nguyên.
BODY_EDGE_FRACTION = 0.002
BODY_HOLE_MAX_FRACTION = 0.015


def solidify_body(alpha, body_box):
    """Làm đặc phần thân SP bị rembg để nửa trong suốt / thủng lỗ nhỏ (xem BODY_EDGE_FRACTION)."""
    from scipy import ndimage

    alpha = np.asarray(alpha)
    height, width = alpha.shape
    x0, y0, x1, y1 = body_box
    edge = max(2, round(BODY_EDGE_FRACTION * max(height, width)))
    # Xét rộng ra ngoài khung để dải viền mềm chỉ theo mép túi thật.
    wx0, wy0, wx1, wy1 = max(0, x0 - edge), max(0, y0 - edge), min(width, x1 + edge), min(height, y1 + edge)
    solid = alpha[wy0:wy1, wx0:wx1] >= OPAQUE
    holes = ndimage.binary_fill_holes(solid) & ~solid
    labels, count = ndimage.label(holes)
    if count:
        sizes = ndimage.sum(holes, labels, range(1, count + 1))
        small = np.flatnonzero(sizes <= BODY_HOLE_MAX_FRACTION * (x1 - x0) * (y1 - y0)) + 1
        solid |= np.isin(labels, small)
    core = ndimage.binary_erosion(solid, iterations=edge)
    inside = np.zeros_like(core)
    inside[y0 - wy0:y1 - wy0, x0 - wx0:x1 - wx0] = True
    out = alpha.copy()
    out[wy0:wy1, wx0:wx1][core & inside] = 255
    return out


def isolate_product(alpha, product_box, prop_boxes):
    """Chỉ giữ phần rembg thuộc SP: bỏ ngoài khung SP, và đoạn thanh giá mảnh lộ trong khe quai (ngoài hàng thân)."""
    alpha = np.asarray(alpha)
    height, width = alpha.shape
    pad = round(PRODUCT_BOX_MARGIN * max(width, height))
    bx0, by0, bx1, by1 = product_box
    x0, y0, x1, y1 = max(0, bx0 - pad), max(0, by0 - pad), min(width, bx1 + pad), min(height, by1 + pad)
    out = np.zeros_like(alpha)
    out[y0:y1, x0:x1] = alpha[y0:y1, x0:x1]
    # Trong dải nới, điểm thuộc khung đạo cụ là đạo cụ.
    margin = np.zeros(alpha.shape, dtype=bool)
    margin[y0:y1, x0:x1] = True
    margin[by0:by1, bx0:bx1] = False
    for px0, py0, px1, py1 in prop_boxes:
        zone = margin[py0:py1, px0:px1]
        out[py0:py1, px0:px1][zone] = 0

    box_width = max(1, x1 - x0)
    prop_pad = max(2, round(0.004 * max(width, height)))
    for px0, py0, px1, py1 in prop_boxes:
        if px1 - px0 > THIN_PROP_MAX_WIDTH * box_width or (py1 - py0) < 2 * (px1 - px0):
            continue
        ex0, ex1 = max(0, px0 - prop_pad), min(width, px1 + prop_pad)
        for y in range(max(y0, py0), min(y1, py1)):
            line = out[y] >= OPAQUE
            if line[x0:x1].mean() > BODY_ROW_COVERAGE:
                continue
            edges = np.diff(np.concatenate(([0], line.astype(np.int8), [0])))
            for start, end in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
                if start >= ex0 and end <= ex1:
                    out[y, start:end] = 0
    return out


# ═══════════════════════════════════════════════════════════════
# Chế độ 2 — Gemini thay SP trên tay chị Nhạm
# ═══════════════════════════════════════════════════════════════

# Thứ tự ảnh khớp view: person trước, product sau.
HELD_PRODUCT_PROMPT = (
    "You are editing a real product marketing photo.\n"
    "IMAGE 1 is the base photo: a woman holding a product. IMAGE 2 is a NEW product "
    "(a supplier/catalog photo, usually on a plain background).\n"
    "TASK: Replace the product she is holding in IMAGE 1 with the product from IMAGE 2. Output "
    "one edited version of IMAGE 1.\n"
    "KEEP UNCHANGED: her face, facial features, expression, skin tone, hair, body, pose, "
    "clothing, the background, camera angle, framing, aspect ratio and overall lighting of "
    "IMAGE 1. Do not add or remove any person or object other than the swapped product. The "
    "old product must disappear completely.\n"
    "NEW PRODUCT FIDELITY: Reproduce the product from IMAGE 2 exactly — same shape, proportions, "
    "colors, materials, packaging, printed text and logo. Do not invent, translate, blur or alter "
    "any text or logo. Do not bring the plain background of IMAGE 2 into the photo.\n"
    "HANDS AND SCALE: Her hands must hold the new product naturally, with a correct grip, the "
    "correct number of fingers and believable contact points; fingers may overlap the product "
    "realistically. Give the product a believable real-world size relative to her hands and "
    "body — do not simply copy the size of the old product if the new one is bigger or smaller.\n"
    "LIGHTING: Match the scene's light direction, color temperature, shadows and reflections so "
    "the product looks photographed in the same shot.\n"
    "QUALITY: Photorealistic and sharp. No cartoon, CGI or illustration look. No watermark, no "
    "added text."
)


def normalize_note(note):
    """Ghi chú tự do của người dùng (vd kích thước thật của SP) — cắt khoảng trắng, chặn độ dài."""
    text = (note or '').strip() if isinstance(note, str) else ''
    if len(text) > HELD_PRODUCT_NOTE_MAX_CHARS:
        raise ProductImageInputError(
            f'Ghi chú tối đa {HELD_PRODUCT_NOTE_MAX_CHARS} ký tự (đang có {len(text)}).'
        )
    return text


def _with_note(prompt, note):
    """Prompt cố định + ghi chú người dùng ở cuối (bổ sung, không thay ràng buộc)."""
    text = normalize_note(note)
    if not text:
        return prompt
    return (
        f"{prompt}\n"
        f"ADDITIONAL NOTES FROM THE CONTENT TEAM (may be written in Vietnamese; follow them as long "
        f"as they do not conflict with the rules above): {text}"
    )


def build_held_product_prompt(note):
    return _with_note(HELD_PRODUCT_PROMPT, note)


# ═══════════════════════════════════════════════════════════════
# Chế độ 3 — Gemini đặt SP vào ảnh cảnh (tạo hàng loạt)
# ═══════════════════════════════════════════════════════════════

# Prompt người dùng viết, giữ nguyên văn. Thứ tự ảnh khớp view: product trước, scene sau.
SCENE_PLACEMENT_PROMPT = (
    "Image 1 may contain the handbag with or without its original background. Extract only the "
    "handbag itself (including its strap, handles, hardware, charms, scarf or hang tag if attached). "
    "Discard everything else from image 1: background, surface, props, original shadows and "
    "reflections.\n\n"
    "Place this handbag into the scene of image 2 on the most natural flat surface in the foreground "
    "center, standing upright, front view, as if it was photographed in that exact location by a "
    "professional product photographer.\n\n"
    "Keep the handbag 100% identical to image 1: same shape, leather color, grain texture, stitching, "
    "hardware and proportions. Do not redesign or recolor it.\n"
    "Keep the scene of image 2 unchanged.\n\n"
    "Analyze the lighting in image 2 (direction, softness, color temperature, light sources) and "
    "relight the handbag to match it exactly. Remove any lighting or color cast from the original "
    "photo of the bag.\n"
    "Match the camera perspective, eye level and lens of image 2. Scale the bag realistically "
    "relative to the objects around it.\n"
    "Add a natural soft contact shadow under the bag consistent with the scene's light direction, "
    "and a subtle reflection only if the surface is glossy.\n"
    "The bag is the sharp focal point with a shallow depth of field; the background is softly "
    "blurred. Bag fills about 45-55% of the frame height. Photorealistic, no CGI look."
)


def build_scene_placement_prompt(note):
    return _with_note(SCENE_PLACEMENT_PROMPT, note)


def read_gemini_image_model():
    """Model tạo ảnh dùng chung với ảnh thẻ (GEMINI_MODEL). Không có mặc định."""
    model_name = (os.environ.get(GEMINI_MODEL_ENV) or '').strip()
    if not model_name:
        raise ProductImageConfigError(
            f'{GEMINI_MODEL_ENV} chưa được set trên AI Service (model tạo ảnh của Gemini).'
        )
    return model_name


_PIL_FORMAT_TO_MIME = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}


def detect_image_mime(image_bytes):
    """Loại ảnh đọc từ chính nội dung file — dùng khi Gemini không ghi mime_type kèm ảnh."""
    try:
        image_format = Image.open(io.BytesIO(image_bytes)).format
    except Exception:
        return None
    return _PIL_FORMAT_TO_MIME.get(image_format)


def extract_generated_image(response):
    """→ (image_bytes | None, mime | None, các đoạn chữ, finish_reason). Không có ảnh thì đoạn chữ là lý do."""
    candidates = getattr(response, 'candidates', None) or []
    if not candidates:
        return None, None, [], None
    first = candidates[0]
    parts = getattr(getattr(first, 'content', None), 'parts', None) or []
    image_bytes = None
    image_mime = None
    texts = []
    for part in parts:
        inline = getattr(part, 'inline_data', None)
        if image_bytes is None and inline is not None and getattr(inline, 'data', None):
            image_bytes = inline.data
            image_mime = (getattr(inline, 'mime_type', None) or '').strip().lower() or detect_image_mime(image_bytes)
            continue
        text = getattr(part, 'text', None)
        if text and text.strip():
            texts.append(text.strip())
    return image_bytes, image_mime, texts, getattr(first, 'finish_reason', None)
