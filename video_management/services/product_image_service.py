"""
Tạo ảnh cho sản phẩm MỚI (chưa về hàng nên media chưa chụp được) để làm content trước.

Hai chế độ, chọn theo nhu cầu ảnh chứ không theo sản phẩm:

1. Ghép background — 0đ. rembg tách nền ảnh SP của nhà cung cấp/catalog (thường nền trắng),
   trả PNG nền trong suốt đã cắt sát mép. Việc dán lên ảnh phòng media chụp sẵn làm ở FE
   (canvas) để người dùng kéo/thu phóng và thấy trước đúng ảnh sẽ xuất ra — phần cần máy chủ
   chỉ là tách nền, nên AI service chỉ làm đúng phần đó.
2. Chị Nhạm cầm SP — tính phí Gemini mỗi lượt. Không dán đè được vì tay phải ôm lấy sản phẩm
   cho tự nhiên, nên đưa Gemini ảnh chị Nhạm đang cầm SP cũ (source media có sẵn) + ảnh SP mới
   và yêu cầu thay SP. Tận dụng ảnh thật làm nền nên không phải sinh người/bối cảnh từ đầu.

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
# trả về phình vài chục MB mà không nét hơn. 2048px vẫn dư cho ảnh content 1080px.
MAX_CUTOUT_SIDE_PX = 2048

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

HELD_PRODUCT_NOTE_MAX_CHARS = 500

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
    mime = (mime_type or '').strip().lower()
    if not image_base64:
        raise ProductImageInputError(f'Thiếu {field_label}.')
    if not mime:
        raise ProductImageInputError(f'Thiếu mime_type của {field_label}.')
    if mime not in ALLOWED_INPUT_MIME_TYPES:
        raise ProductImageInputError(
            f'{field_label.capitalize()} phải là JPG, PNG hoặc WebP (nhận được: {mime}).'
        )
    try:
        # validate=True: chuỗi không phải base64 thì từ chối ngay, không âm thầm giải mã ra rác.
        data = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ProductImageInputError(f'{field_label.capitalize()} không giải mã được (base64 hỏng).')
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
    return rgba.crop((left, top, right, bottom))


def cut_out_product(image_bytes, remove_background):
    """Tách nền ảnh SP, trả về (png_bytes, width, height) của SP đã cắt sát mép.

    `remove_background(PIL.Image) -> PIL.Image RGBA` là bước rembg; truyền vào thay vì gọi thẳng
    để test được phần xoay/thu nhỏ/cắt mép mà không cần tải model.
    """
    image = open_upright_image(image_bytes, 'ảnh sản phẩm')
    # rembg làm việc trên RGB; ảnh PNG có sẵn alpha (SP đã tách nền từ trước) vẫn chạy lại để
    # mặt nạ đồng nhất, convert trước để ảnh P/LA/CMYK không làm rembg lỗi.
    image = downscale_to_max_side(image.convert('RGB'))
    cutout = trim_to_content(remove_background(image))
    buffer = io.BytesIO()
    cutout.save(buffer, format='PNG')
    return buffer.getvalue(), cutout.width, cutout.height


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


def build_held_product_prompt(note):
    """Prompt cố định + ghi chú của người dùng (nếu có) ở cuối.

    Ghi chú để cuối và gắn nhãn rõ là thông tin bổ sung — ví dụ "hộp cao khoảng 30cm" — để nó
    bổ sung chứ không thay các ràng buộc giữ nguyên người/bối cảnh ở trên.
    """
    text = normalize_note(note)
    if not text:
        return HELD_PRODUCT_PROMPT
    return (
        f"{HELD_PRODUCT_PROMPT}\n"
        f"ADDITIONAL NOTES FROM THE CONTENT TEAM (may be written in Vietnamese; follow them as long "
        f"as they do not conflict with the rules above): {text}"
    )


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
