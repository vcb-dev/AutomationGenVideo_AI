"""
Tạo ảnh cho sản phẩm MỚI (chưa về hàng nên media chưa chụp được) để làm content trước.

Hai chế độ, chọn theo nhu cầu ảnh chứ không theo sản phẩm:

1. Ghép background (0đ) — rembg tách nền ảnh SP của nhà cung cấp/catalog, model nhận diện tự huấn
   luyện (product_detector.py, chạy ngay trên AI service) chỉ giữ đúng SP, bỏ giá trưng bày/đạo cụ;
   trả PNG nền trong suốt đã cắt sát mép. Việc dán lên ảnh phòng làm ở FE (canvas, tự động) — phần
   cần máy chủ chỉ là tách đúng SP.
2. Chị Nhạm cầm SP — tính phí Gemini mỗi lượt. Không dán đè được vì tay phải ôm lấy sản phẩm
   cho tự nhiên, nên đưa Gemini ảnh chị Nhạm đang cầm SP cũ (source media có sẵn) + ảnh SP mới
   và yêu cầu thay SP. Tận dụng ảnh thật làm nền nên không phải sinh người/bối cảnh từ đầu.
3. Đặt SP vào cảnh — tính phí Gemini mỗi lượt, dùng cho tạo ảnh hàng loạt không chỉnh tay: đưa
   Gemini ảnh SP + ảnh cảnh, Gemini tự tách SP, đặt lên mặt phẳng, khớp ánh sáng/phối cảnh/bóng.

Vô trạng thái, cùng kiến trúc id_photo_views.py: BE orchestrate và gửi ảnh base64 inline, AI
service không lưu gì. File này giữ phần thuần logic (kiểm đầu vào, cắt mép, dựng prompt, đọc
phản hồi Gemini) để test được mà không cần rembg/Gemini thật.
"""
import base64
import binascii
import io
import os
import threading

import numpy as np
from PIL import Image, ImageOps

# BE chặn 10MB ở bước upload; chặn thêm ở đây phòng bên gọi khác (defense in depth) — cùng mốc
# với id_photo_views.MAX_INPUT_IMAGE_MB.
MAX_INPUT_IMAGE_MB = 12

# Ảnh NCC/catalog hay là WebP (tải từ web) nên nhận thêm WebP — cả Pillow, rembg lẫn Gemini
# đều đọc được, khác ảnh thẻ chỉ nhận ảnh chụp JPG/PNG.
ALLOWED_INPUT_MIME_TYPES = {'image/jpeg', 'image/jpg', 'image/png', 'image/webp'}

# Ảnh SP to hơn mốc này thì thu nhỏ trước khi tách nền: rembg dự đoán mặt nạ ở độ phân giải cố
# định của model rồi phóng về kích thước ảnh, nên ảnh 6000px chỉ tốn thêm RAM/CPU và làm PNG
# trả về phình vài chục MB mà không nét hơn. Đo 15/10 trên 15 ảnh chụp thật của shop (1707×2560, cùng cỡ ảnh nền ghép):
# mốc cũ 2048 thu túi còn 80% rồi khi ghép phải phóng lại → mất ~13% chi tiết vân da, điểm bóng tối đi 1,6 L*. Mốc này
# giữ nguyên ảnh máy ảnh shop và đủ để túi trên ảnh ghép 2560px không phải phóng.
MAX_CUTOUT_SIDE_PX = 3072

# Điểm ảnh có alpha thấp hơn mốc này coi là nền khi cắt sát mép — rembg hay để lại quầng mờ
# alpha 1-10 quanh vật thể, tính cả vào thì khung cắt rộng ra và SP bị lệch tâm khi ghép.
TRIM_ALPHA_THRESHOLD = 16

# Dải hàng/cột tách rời khỏi SP (cách nhau bởi hàng/cột trống) mà chiếm ít hơn tỉ lệ này tổng
# KHỐI LƯỢNG ALPHA của SP thì coi là vụn nền và không tính vào khung cắt. Đo thật trên ảnh catalog
# bizweb: dòng logo "VIỄN CHÍ BẢO" in trên nền trắng còn sót lại mờ phía trên chiếc nhẫn, khung
# cắt vì thế cao 713px trong khi nhẫn chỉ ~480px. Tính theo SỐ điểm ảnh thì dòng logo chiếm 2,4%
# (lọt ngưỡng), tính theo alpha chỉ 0,3% (alpha tối đa 64, nhẫn gần như toàn 255) — nên cân theo
# alpha. Dây chuyền mảnh vẫn liền dải với mặt dây nên không bị cắt nhầm.
TRIM_MIN_BAND_FRACTION = 0.01

# Ảnh gốc không đủ làm ảnh thương mại (báo người dùng, vẫn ghép): SP chạm mép ảnh — dòng/cột ngoài cùng có điểm SP
# đặc (alpha ≥ CUT_OFF_ALPHA) nhiều hơn tỉ lệ này của cạnh → ảnh gốc chụp/cắt mất phần túi ở mép đó, ghép lên nền thấy
# túi bị cụt thẳng (gặp 15/10: ảnh lưu từ Google cắt mất nửa phải túi).
CUT_OFF_ALPHA = 128
CUT_OFF_EDGE_FRACTION = 0.02
# Cạnh dài của SP tính theo điểm ảnh GỐC nhỏ hơn mốc này thì khi ghép phải phóng hơn ~1,5 lần (túi trên nền dọc 2560px
# cao ~1100px) → mờ, bệt dù đã phóng bằng model. Ảnh chụp máy ảnh của shop túi ~1000–1500px; ảnh lưu từ Google ~150–250px.
SHARP_PRODUCT_MIN_SIDE_PX = 700
_SIDE_LABELS = {'top': 'trên', 'bottom': 'dưới', 'left': 'trái', 'right': 'phải'}

HELD_PRODUCT_NOTE_MAX_CHARS = 500
# Bề ngang thật thân túi (cm) người dùng nhập cho tab Chị Nhạm — ngoài khoảng này coi là gõ nhầm (ví ~10cm, túi lớn ~40cm;
# khớp SIZE_CM_RANGE bên FE).
WIDTH_CM_MIN = 6
WIDTH_CM_MAX = 60

# Tên biến môi trường — đọc lúc DÙNG, không có giá trị mặc định (thiếu thì báo đúng tên biến).
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
    """Giải mã 1 ảnh base64 do BE gửi, trả về (bytes, mime_type đã chuẩn hoá).

    `field_label` là tên ảnh hiển thị trong thông báo lỗi ("ảnh sản phẩm", ...) để người dùng
    biết đúng ảnh nào hỏng khi một request mang hai ảnh.
    """
    if not image_base64:
        raise ProductImageInputError(f'Thiếu {field_label}.')
    mime = _check_input_mime(mime_type, field_label)
    try:
        # validate=True: chuỗi không phải base64 thì từ chối ngay, không âm thầm giải mã ra rác.
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
    """Mở ảnh bằng Pillow và xoay theo EXIF — ảnh chụp điện thoại lưu nằm ngang kèm cờ xoay,
    bỏ qua cờ này thì SP tách ra bị nằm ngang."""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception:
        raise ProductImageInputError(f'{field_label.capitalize()} không phải file ảnh đọc được.')
    return ImageOps.exif_transpose(image)


# ═══════════════════════════════════════════════════════════════
# Chế độ 1 — tách nền ảnh SP bằng rembg
# ═══════════════════════════════════════════════════════════════

# Ảnh có kênh alpha mà điểm thấp nhất dưới mốc này thì coi là đã có nền trong suốt (ảnh PNG đặc hoàn toàn lưu kèm alpha
# 255 thì không tính).
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
    """Session ONNX của rembg, giữ lại theo tên model trong suốt vòng đời worker.

    Nạp model tốn vài giây và vài trăm MB RAM — tạo mới mỗi request thì mỗi lần bấm "Tách nền"
    lại chờ nạp lại. Lần đầu tiên rembg tự tải file model về U2NET_HOME nếu chưa có sẵn
    (Dockerfile.railway tải sẵn lúc build khi có REMBG_MODEL). Khoá để 4 thread của cùng một
    worker gunicorn không nạp trùng một model cùng lúc.
    """
    with _rembg_sessions_lock:
        session = _rembg_sessions.get(model_name)
        if session is None:
            # Import muộn: rembg kéo theo onnxruntime/scipy/scikit-image (~500MB RAM mỗi worker),
            # nạp sẵn lúc khởi động thì worker nào cũng gánh kể cả khi không ai dùng tính năng này.
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
    """Tắt vùng nhớ đệm (arena) của onnxruntime.

    Mặc định onnxruntime giữ lại bộ nhớ đã cấp cho lượt suy luận trước để dùng lại. Đo thật với
    isnet-general-use trên image Dockerfile.railway: worker giữ ~2,2GB sau vài ảnh; tắt arena thì
    còn ~0,95GB (đỉnh 1,47GB) mà thời gian mỗi ảnh không đổi (~1-1,5s). Mỗi worker gunicorn giữ
    một session riêng nên phần tiết kiệm nhân theo số worker.
    """
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
    """Khoảng [đầu, cuối) phủ mọi dải liền mạch có đủ khối lượng trên một trục.

    `mass[i]` là tổng alpha của các điểm ảnh SP ở hàng/cột i. Dải = các chỉ số liên tiếp có
    mass > 0; dải nào nhỏ hơn `min_fraction` tổng khối lượng thì bỏ (vụn nền), còn lại lấy từ dải
    giữ đầu tiên tới dải giữ cuối cùng — hai món tách rời cỡ ngang nhau (đôi bông tai) vẫn được
    giữ cả hai.
    """
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
    """Cắt ảnh RGBA sát mép SP, bỏ qua vụn nền nhỏ tách rời.

    Cắt sát để FE đặt SP theo đúng kích thước thật của nó: thanh "Kích thước" và vị trí tâm
    tính trên khung SP chứ không trên cả khoảng nền trong suốt thừa quanh nó.
    """
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
    """Tách nền ảnh SP, trả về (png_bytes, width, height, khung thân SP | None) của SP đã cắt sát mép.

    `remove_background(PIL.Image) -> PIL.Image RGBA` là bước rembg; `locate_product(PIL.Image, mặt nạ
    alpha rembg) -> (khung SP, [khung đạo cụ], khung thân SP | None[, mặt nạ phần SP ngoài khung cần giữ])`
    theo pixel là bước nhận diện (model
    tự huấn luyện, ảnh nào model không chắc thì Gemini) — cả hai truyền vào thay vì gọi thẳng để test
    được phần xoay/thu nhỏ/lọc/cắt mép mà không cần model thật. `upscale(PIL.Image) -> (PIL.Image, hệ số)` phóng
    ảnh SP nhỏ trước khi tách nền (product_upscale.upscale_if_small). `info` (dict, nếu truyền) nhận: kích thước ảnh
    gốc, hệ số phóng, kết quả làm sạch mép (cutout_refine).
    Khung thân SP (túi không tính quai/dây/khăn) trả theo toạ độ ảnh đã cắt sát mép — FE dùng để mọi
    túi cùng cỡ thân và đáy thân đứng đúng mặt hộp, dù túi có dây dài hay quai cao.
    """
    from video_management.services import cutout_refine

    image = open_upright_image(image_bytes, 'ảnh sản phẩm')
    # rembg làm việc trên RGB; ảnh PNG/WebP có sẵn nền trong suốt (SP đã tách nền từ trước) vẫn chạy lại để mặt nạ
    # đồng nhất — nhưng phải đặt lên nền trắng chứ không convert('RGB') thẳng: màu dưới vùng trong suốt là rác của bộ
    # mã hoá (ảnh WebP người dùng thả 15/10 ra cả mảng sọc ngang trong lòng quai), rembg giữ luôn mảng đó.
    source_alpha = transparency_of(image)
    image = flatten_on_white(image) if source_alpha is not None else image.convert('RGB')
    source_size = image.size
    factor = 1
    if upscale is not None:
        image, factor = upscale(image)
    image = downscale_to_max_side(image)
    cutout = remove_background(image).convert('RGBA')
    if source_alpha is not None:
        # Vùng ảnh gốc đã trong suốt thì chắc chắn là nền — rembg có giữ nhầm cũng bỏ.
        source_alpha = np.asarray(source_alpha.resize(image.size, Image.BILINEAR))
        cutout.putalpha(Image.fromarray(np.minimum(np.asarray(cutout.getchannel('A')), source_alpha)))
    rembg_alpha = np.asarray(cutout.getchannel('A'))
    # locate_product trả None khi không nhận diện được (Gemini hết hạn mức...) → giữ kết quả rembg.
    located = locate_product(image, np.asarray(cutout.getchannel('A'))) if locate_product is not None else None
    body_box = None
    if located is not None:
        # Phần tử thứ 4 (nếu có): mặt nạ phần SP ngoài khung cần giữ — quai/dây model khoanh hụt.
        product_box, prop_boxes, body_box, *keep = located
        original = np.asarray(cutout.getchannel('A'))
        alpha = isolate_product(original, product_box, prop_boxes)
        if keep and keep[0] is not None:
            alpha = np.where(keep[0], original, alpha)
        if body_box is not None:
            solid = solidify_body(alpha, body_box)
            raised = solid > alpha
            if raised.any():
                # rembg trộn màu điểm nửa trong suốt với nền ĐEN (Image.composite với ảnh rỗng) — làm đặc
                # mà giữ màu đó thì túi kem thành xám xỉn (gặp 10/10), nên lấy lại màu thật từ ảnh gốc.
                rgba = np.array(cutout)
                rgba[..., :3][raised] = np.asarray(image)[raised]
                cutout = Image.fromarray(rgba, 'RGBA')
            alpha = solid
        cutout.putalpha(Image.fromarray(alpha))
    if source_alpha is not None:
        # Làm đặc thân (solidify_body) theo khung thân model — khung lố ra khe giữa thân và dây thì khe thành mảng TRẮNG
        # (màu nền đã đặt) — vùng ảnh gốc trong suốt luôn thắng.
        cutout.putalpha(Image.fromarray(np.minimum(np.asarray(cutout.getchannel('A')), source_alpha)))
    # Làm sạch trên ảnh gốc (không lấy màu rembg đã nhân độ đặc): xoá phông giữ nhầm, tính lại mép, khử ám phông.
    rgba, refine = cutout_refine.refine_cutout(np.asarray(image), np.asarray(cutout.getchannel('A')), rembg_alpha, body_box)
    if source_alpha is not None:
        rgba = rgba.copy()
        keep = np.minimum(rgba[..., 3], source_alpha)
        # Ảnh ra đã nhân độ đặc (premultiplied) → giảm alpha thì giảm màu theo cùng tỉ lệ.
        scale = np.where(rgba[..., 3] > 0, keep / np.maximum(rgba[..., 3], 1), 0)[..., None]
        rgba[..., :3] = np.round(rgba[..., :3] * scale).astype(np.uint8)
        rgba[..., 3] = keep
    cutout = Image.fromarray(rgba, 'RGBA')
    left, top, right, bottom = content_box(cutout)
    if info is not None:
        info.update(
            source_size=list(source_size), upscaled=factor, refine=refine,
            cut_off_sides=cut_off_sides(rgba[..., 3]),
            # Ảnh đã phóng/thu so với ảnh gốc theo image.width / source_size[0].
            product_source_px=round(max(right - left, bottom - top) * source_size[0] / image.width),
        )
    cutout = cutout.crop((left, top, right, bottom))
    buffer = io.BytesIO()
    cutout.save(buffer, format='PNG')
    return buffer.getvalue(), cutout.width, cutout.height, shift_box(body_box, left, top, cutout.width, cutout.height)


def parse_width_cm(raw):
    """Bề ngang thật thân SP (cm) người dùng nhập → float; trống → None (ghép theo cỡ SP cũ như trước); sai → lỗi 400."""
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
        # Người dùng lấy ảnh mẫu trên mạng (15/10): ảnh lưu thẳng từ trang kết quả Google Hình ảnh là ảnh thu nhỏ ~200px
        # (tên kiểu images.jpeg, 2–3KB) — chỉ cách lấy bản lớn, đừng bảo "chụp bằng máy".
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


# ─── Nhận diện đúng sản phẩm (bỏ giá trưng bày, đạo cụ) ─────────────────────
#
# rembg tách MỌI vật nổi bật trong ảnh, không biết đâu là SP: ảnh NCC chụp túi trên giá trưng bày
# thì giữ luôn cả giá (đo trên ảnh thử: 25% điểm ảnh "túi" thực ra là giá), giá chạm mặt bàn còn túi
# lơ lửng khi ghép; khối mica/lọ thuỷ tinh cạnh túi thành bóng ma. Model nhận diện tự huấn luyện
# (product_detector.py) trả khung của SP (kèm mọi thứ gắn vào SP) và khung từng kệ/đạo cụ — giữ phần
# rembg nằm trong khung SP, bỏ đoạn đạo cụ mảnh lộ ra trong khe quai. Viền vẫn là viền rembg.

# Khung nhận diện lệch vài điểm ảnh — nới thêm để không gọt mất mép quai/dây.
PRODUCT_BOX_MARGIN = 0.015
# Đạo cụ "mảnh" (thanh giá) là khung hẹp so với SP — chỉ loại này mới xoá phần lộ trong khung SP;
# đạo cụ to nằm trong khung SP thì không đụng tới, tránh xoá nhầm quai đi ngang qua nó.
THIN_PROP_MAX_WIDTH = 0.12
# Hàng có điểm SP phủ quá tỷ lệ này bề ngang khung SP là hàng thân SP: thanh giá nằm sau thân,
# điểm đó là SP thật.
BODY_ROW_COVERAGE = 0.6
OPAQUE = 32


# Túi màu sáng (kem, trắng) trên nền sáng: rembg trả thân túi NỬA TRONG SUỐT — đo 2 túi kem người dùng thử
# 10/10: 75–80% điểm trong khung thân có alpha 64–249 (túi tối: ≥250), ghép lên nền thấy xuyên qua thân;
# còn có đốm alpha ~0 rải rác (cạnh dây đai, móc khoá). Thân túi là vật đặc nên trong khung thân: điểm đã là
# SP (alpha ≥ OPAQUE) và lỗ NHỎ bị bao kín được làm đặc hẳn; lỗ to (khe quai, ô rỗng thật) giữ nguyên, và
# chỉ chừa một dải viền mềm ở MÉP TÚI THẬT (không phải mép khung) như rembg cắt.
BODY_EDGE_FRACTION = 0.002
BODY_HOLE_MAX_FRACTION = 0.015


def solidify_body(alpha, body_box):
    """Làm đặc phần thân SP bị rembg để nửa trong suốt / thủng lỗ nhỏ (xem BODY_EDGE_FRACTION)."""
    from scipy import ndimage   # có sẵn: rembg (requirements) cài kèm scipy

    alpha = np.asarray(alpha)
    height, width = alpha.shape
    x0, y0, x1, y1 = body_box
    edge = max(2, round(BODY_EDGE_FRACTION * max(height, width)))
    # Xét rộng ra ngoài khung thân một dải để biết mép nào là mép túi thật (ngoài là nền) và mép nào chỉ là
    # mép khung (túi còn tiếp) — co dải viền theo mép túi thật thôi.
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
    """Chỉ giữ phần rembg thuộc SP.

    1. Bỏ mọi điểm ngoài khung SP (đã nới PRODUCT_BOX_MARGIN): chân giá dưới đáy, đĩa/móc trên
       đỉnh, khối mica bên cạnh; trong dải nới, điểm thuộc khung đạo cụ cũng bỏ.
    2. Thanh giá mảnh chạy xuyên khung SP: ở các hàng KHÔNG phải thân SP (khe giữa quai và miệng
       túi), xoá những đoạn điểm nằm gọn trong khung thanh. Quai vắt ngang qua thanh chạy ra ngoài
       khung thanh nên được giữ; ở hàng thân SP thanh nằm sau thân nên không đụng.
    """
    alpha = np.asarray(alpha)
    height, width = alpha.shape
    pad = round(PRODUCT_BOX_MARGIN * max(width, height))
    bx0, by0, bx1, by1 = product_box
    x0, y0, x1, y1 = max(0, bx0 - pad), max(0, by0 - pad), min(width, bx1 + pad), min(height, by1 + pad)
    out = np.zeros_like(alpha)
    out[y0:y1, x0:x1] = alpha[y0:y1, x0:x1]
    # Dải nới quanh khung SP: điểm nào nằm trong khung đạo cụ thì là đạo cụ (vd mẩu chân giá ngay
    # dưới đáy túi — giữ lại thì túi vẫn lơ lửng đúng bằng chiều cao mẩu đó khi ghép).
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

# Ảnh 1 là nền giữ nguyên, ảnh 2 là SP mới — thứ tự này phải khớp thứ tự đưa vào
# generate_content ở view (person trước, product sau).
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
    """Prompt cố định + ghi chú của người dùng (nếu có) ở cuối.

    Ghi chú để cuối và gắn nhãn rõ là thông tin bổ sung — ví dụ "hộp cao khoảng 30cm" — để nó
    bổ sung chứ không thay các ràng buộc giữ nguyên SP/bối cảnh ở trên.
    """
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

# Prompt người dùng viết (2026-10-08), giữ nguyên văn. Ảnh 1 là SP, ảnh 2 là cảnh — thứ tự này phải
# khớp thứ tự đưa vào generate_content ở view (product trước, scene sau). Ảnh cảnh đứng cuối còn để
# Gemini lấy khổ ảnh ra theo ảnh cảnh.
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
    """Tách ảnh đầu tiên khỏi phản hồi generate_content.

    Trả về (image_bytes | None, mime_type | None, text_parts, finish_reason). Model có thể kèm
    vài dòng chữ trước/sau ảnh; khi KHÔNG có ảnh thì chính dòng chữ đó (lời từ chối, giải thích)
    là manh mối duy nhất vì sao — view đưa nó vào thông báo lỗi thay vì đoán lý do.

    mime_type lấy từ phản hồi chứ không cố định PNG: model tạo ảnh đời sau có thể trả JPEG, gắn
    nhầm PNG thì trình duyệt vẫn hiện nhưng file tải về mang sai đuôi.
    """
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
        # inline_data.data là bytes thô (proto BYTES), không phải chuỗi base64.
        if image_bytes is None and inline is not None and getattr(inline, 'data', None):
            image_bytes = inline.data
            image_mime = (getattr(inline, 'mime_type', None) or '').strip().lower() or detect_image_mime(image_bytes)
            continue
        text = getattr(part, 'text', None)
        if text and text.strip():
            texts.append(text.strip())
    return image_bytes, image_mime, texts, getattr(first, 'finish_reason', None)
