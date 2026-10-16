"""
Tạo ảnh sản phẩm mới — 3 endpoint cho 3 chế độ, xem mô tả đầy đủ ở
video_management/services/product_image_service.py.

- POST /api/ai/product-image/cutout/           — rembg tách nền + model nhận diện đúng SP (0đ)
- POST /api/ai/product-image/held-product/     — Gemini thay SP trên tay chị Nhạm (tính phí)
- POST /api/ai/product-image/held-swap/        — đổi SP trên tay chị Nhạm bằng model tự huấn luyện + SAM + LaMa
                                                 (0đ)
- POST /api/ai/product-image/held-auto/        — chỉ ảnh SP mới: tự chọn tư thế chị Nhạm hợp nhất trong thư viện tư thế
                                                 dựng sẵn rồi ghép (0đ)
- POST /api/ai/product-image/scene-placement/  — Gemini đặt SP vào ảnh cảnh (tính phí)

Ảnh gửi dạng file multipart (hoặc `<tên>_base64` trong JSON — kiểu cũ). Mọi endpoint bắt buộc JWT (BE ký theo
người bấm) — URL AI gọi thẳng được từ ngoài.
"""
import base64
import io
import logging
import time

import numpy as np
from PIL import Image

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from video_management.services import held_swap as hs
from video_management.services import pose_library as pl
from video_management.services import product_detector as det
from video_management.services import product_image_cost as cost
from video_management.services import product_image_service as svc
from video_management.services import product_upscale as up

logger = logging.getLogger(__name__)

# Thấp hơn mốc chờ 90s của BE để AI kịp trả lỗi rõ ràng.
HELD_PRODUCT_TIMEOUT_SECONDS = 75


def _input_image(request, field, mime_field, label):
    """Một ảnh BE gửi: file multipart `field`, hoặc `<field>_base64` trong JSON (kiểu cũ, ảnh nhỏ hơn 2,5MB).
    """
    try:
        upload = request.FILES.get(field)
        mime_type = request.data.get(mime_field)
        if upload is None:
            return svc.decode_input_image(request.data.get(f'{field}_base64'), mime_type, label)
    except RequestDataTooBig:
        raise svc.ProductImageInputError(
            f'Ảnh gửi sang AI quá lớn để gửi dạng base64 trong JSON — BE phải gửi {label} dạng file (multipart).'
        )
    return svc.check_input_image(upload.read(), mime_type or upload.content_type, label)


def _error(message, status, usage=None):
    body = {'success': False, 'error_message': message}
    if usage is not None:
        # Lượt đã chạm tới Gemini thì kể cả lỗi vẫn báo token/chi phí để BE ghi vào thống kê.
        body['usage'] = usage
    return Response(body, status=status)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cutout(request):
    """
    Body (multipart): image=<file>, mime_type=image/jpeg  (hoặc JSON kiểu cũ { "image_base64", "mime_type" })
    Response 200: { "success": true, "cutout_image_base64": "<PNG>", "width": 812, "height": 1240,
                    "model": "<rembg>", "usage": {model tự huấn luyện, 0đ},
                    "detection": {"status": "applied", "source": "model", "props_removed": 3[, "model_reasons": [...]]}
                               | {"status": "skipped", "source": "model", "reason": "...", "model_reasons": [...]},
                    "body_box": [x0, y0, x1, y1] | null, "source_size": [w, h], "upscaled": 1 | 4,
                    "quality_warnings": ["Ảnh gốc cắt mất một phần sản phẩm ở mép phải — ..."] }
    "skipped" = còn phần dính liền túi model nghi là kệ, FE cảnh báo.
    """
    try:
        image_bytes, _ = _input_image(request, 'image', 'mime_type', 'ảnh sản phẩm')
        model_name = svc.read_rembg_model()
        detector = det.get_detector(det.read_detector_model_path())
        upscaler = up.get_upscaler(up.read_upscale_model_path())
        session = svc.get_rembg_session(model_name)
    except svc.ProductImageInputError as e:
        return _error(str(e), 400)
    except svc.ProductImageConfigError as e:
        logger.error(f"[ProductImage Cutout] Cấu hình: {e}")
        return _error(str(e), 500)
    except Exception as e:
        # Lần đầu nạp model, rembg tải file model từ GitHub — mạng lỗi thì rơi vào đây.
        logger.exception(f"[ProductImage Cutout] Không nạp được model tách nền: {e}")
        return _error(f'Không nạp được model tách nền ({type(e).__name__}): {e}', 502)

    from rembg import remove

    t0 = time.time()
    usage = cost.usage_free(detector.name)
    seen = {}
    info = {}

    def locate(image, alpha):
        located, seen['detection'] = det.decide_cutout(alpha, det.detect_product(detector, image, alpha))
        return located

    try:
        png_bytes, width, height, body_box = svc.cut_out_product(
            image_bytes, lambda image: remove(image, session=session), locate,
            upscale=lambda image: up.upscale_if_small(upscaler, image), info=info,
        )
    except svc.ProductImageInputError as e:
        return _error(str(e), 400, usage)
    except Exception as e:
        logger.exception(f"[ProductImage Cutout] Lỗi tách nền: {e}")
        return _error(f'Lỗi tách nền: {e}', 500, usage)

    detection = seen['detection']
    log = logger.warning if detection['status'] == 'skipped' else logger.info
    log(
        f"[ProductImage Cutout] model={model_name} {width}x{height} "
        f"{len(png_bytes) / 1024:.0f}KB sau {time.time() - t0:.1f}s, nhận diện {detection}, {info}"
    )
    return Response({
        'success': True,
        'cutout_image_base64': base64.b64encode(png_bytes).decode('ascii'),
        'width': width,
        'height': height,
        'model': model_name,
        'usage': usage,
        'detection': detection,
        # Khung thân SP trong ảnh tách nền [x0, y0, x1, y1] — FE canh cỡ/đáy theo thân, không theo dây/quai.
        'body_box': body_box,
        # Cỡ ảnh gốc + hệ số phóng (1 = không phóng) — FE báo khi ảnh gốc quá nhỏ (ảnh lưu từ Google ~200px).
        'source_size': info.get('source_size'),
        'upscaled': info.get('upscaled', 1),
        # Ảnh gốc không đủ làm ảnh thương mại (túi bị cắt ở mép, túi quá nhỏ) — FE báo cạnh ảnh, vẫn ghép.
        'quality_warnings': svc.input_quality_warnings(info),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def held_product(request):
    """
    Body (multipart, hoặc JSON base64 kiểu cũ): {
      "person_image_base64": "...", "person_mime_type": "image/jpeg",   // chị Nhạm cầm SP cũ
      "product_image_base64": "...", "product_mime_type": "image/png",  // SP mới
      "note": "hộp cao khoảng 30cm"                                     // không bắt buộc
    }
    Response 200: { "success": true, "image_base64": "...", "mime_type": "image/png", "usage": {...} }
    `usage` (có cả trong phản hồi lỗi khi đã gọi tới Gemini) — xem product_image_cost.py:
      { "model", "input_tokens", "output_tokens", "cost_usd": số | null, "cost_note": lý do | null }
    """
    try:
        person_bytes, person_mime = _input_image(request, 'person_image', 'person_mime_type', 'ảnh chị Nhạm cầm sản phẩm')
        product_bytes, product_mime = _input_image(request, 'product_image', 'product_mime_type', 'ảnh sản phẩm mới')
        prompt = svc.build_held_product_prompt(request.data.get('note'))
        model_name = svc.read_gemini_image_model()
    except svc.ProductImageInputError as e:
        return _error(str(e), 400, cost.usage_not_billed(None, 'Đầu vào sai, chưa gọi Gemini — không tính phí.'))
    except svc.ProductImageConfigError as e:
        logger.error(f"[ProductImage HeldProduct] Cấu hình: {e}")
        return _error(str(e), 500, cost.usage_not_billed(None, 'Thiếu cấu hình, chưa gọi Gemini — không tính phí.'))

    return _generate_image(
        'HeldProduct',
        [{'mime_type': person_mime, 'data': person_bytes}, {'mime_type': product_mime, 'data': product_bytes}],
        prompt,
        model_name,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def held_swap(request):
    """
    Body (multipart, hoặc JSON base64 kiểu cũ): {
      "person_image_base64": "...", "person_mime_type": "image/jpeg",   // chị Nhạm cầm SP cũ
      "product_image_base64": "...", "product_mime_type": "image/png"   // SP mới (có hoặc không có nền)
    }
    Response 200: { "success": true, "image_base64": "<JPEG>", "mime_type": "image/jpeg", "usage": {0đ},
                    "info": {"hands": 2, "strap_kept": false, "scale": 1.02, "warnings": [...], ...} }
    0đ: tách nền như cutout rồi held_swap đổi SP.
    """
    try:
        person_bytes, _ = _input_image(request, 'person_image', 'person_mime_type', 'ảnh chị Nhạm cầm sản phẩm')
        product_bytes, _ = _input_image(request, 'product_image', 'product_mime_type', 'ảnh sản phẩm mới')
        person_image = svc.open_upright_image(person_bytes, 'ảnh chị Nhạm cầm sản phẩm')
        rembg_model = svc.read_rembg_model()
        product_detector = det.get_detector(det.read_detector_model_path())
        upscaler = up.get_upscaler(up.read_upscale_model_path())
        swap_models = hs.get_models(*hs.read_swap_paths())
        session = svc.get_rembg_session(rembg_model)
    except svc.ProductImageInputError as e:
        return _error(str(e), 400)
    except svc.ProductImageConfigError as e:
        logger.error(f"[ProductImage HeldSwap] Cấu hình: {e}")
        return _error(str(e), 500)
    except Exception as e:
        logger.exception(f"[ProductImage HeldSwap] Không nạp được model: {e}")
        return _error(f'Không nạp được model ({type(e).__name__}): {e}', 502)

    from rembg import remove

    t0 = time.time()
    usage = cost.usage_free(f"{product_detector.name}+{swap_models['person'].name}")

    def locate(image, alpha):
        located, _ = det.decide_cutout(alpha, det.detect_product(product_detector, image, alpha))
        return located

    try:
        png_bytes, width, height, body_box = svc.cut_out_product(
            product_bytes, lambda image: remove(image, session=session), locate,
            upscale=lambda image: up.upscale_if_small(upscaler, image),
        )
        product_rgba = np.asarray(Image.open(io.BytesIO(png_bytes)).convert('RGBA'))
        # Model SP không thấy thân (ảnh lạ) → coi cả SP đã cắt là thân.
        image_bytes, info = hs.swap_product(swap_models, person_image, product_rgba, body_box or [0, 0, width, height])
    except svc.ProductImageInputError as e:
        return _error(str(e), 400, usage)
    except Exception as e:
        logger.exception(f"[ProductImage HeldSwap] Lỗi đổi SP: {e}")
        return _error(f'Lỗi đổi sản phẩm: {e}', 500, usage)

    logger.info(f"[ProductImage HeldSwap] {info['width']}x{info['height']} sau {time.time() - t0:.1f}s, {info}")
    return Response({
        'success': True,
        'image_base64': base64.b64encode(image_bytes).decode('ascii'),
        'mime_type': 'image/jpeg',
        'usage': usage,
        'info': info,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def held_auto(request):
    """
    Body (multipart): product_image=<file>, product_mime_type=image/png, width_cm=24  (hoặc JSON kiểu cũ
                      product_image_base64) — width_cm: bề ngang thật thân túi
    Response 200: { "success": true, "usage": {0đ},
                    "candidates": [{"image_base64": "<JPEG>", "mime_type": "image/jpeg",
                                    "info": {"pose_id", "score", "hands", "warnings", "real_size", ...}}, ...] }
    0đ: tách nền rồi ghép vào các tư thế hợp nhất trong thư viện (pose_library.compose_best).
    """
    try:
        product_bytes, _ = _input_image(request, 'product_image', 'product_mime_type', 'ảnh sản phẩm mới')
        width_cm = svc.parse_width_cm(request.data.get('width_cm'))
        folder, count = pl.read_library_settings()
        rembg_model = svc.read_rembg_model()
        product_detector = det.get_detector(det.read_detector_model_path())
        upscaler = up.get_upscaler(up.read_upscale_model_path())
        swap_models = hs.get_models(*hs.read_swap_paths())
        poses = pl.load_library(folder)
        session = svc.get_rembg_session(rembg_model)
    except svc.ProductImageInputError as e:
        return _error(str(e), 400)
    except svc.ProductImageConfigError as e:
        logger.error(f"[ProductImage HeldAuto] Cấu hình: {e}")
        return _error(str(e), 500)
    except Exception as e:
        logger.exception(f"[ProductImage HeldAuto] Không nạp được model/thư viện tư thế: {e}")
        return _error(f'Không nạp được model/thư viện tư thế ({type(e).__name__}): {e}', 502)

    from rembg import remove

    t0 = time.time()
    usage = cost.usage_free(f"{product_detector.name}+{swap_models['person'].name}")

    def locate(image, alpha):
        located, _ = det.decide_cutout(alpha, det.detect_product(product_detector, image, alpha))
        return located

    try:
        png_bytes, width, height, body_box = svc.cut_out_product(
            product_bytes, lambda image: remove(image, session=session), locate,
            upscale=lambda image: up.upscale_if_small(upscaler, image),
        )
        product_rgba = np.asarray(Image.open(io.BytesIO(png_bytes)).convert('RGBA'))
        results = pl.compose_best(swap_models, poses, product_rgba, body_box or [0, 0, width, height], count, width_cm)
    except svc.ProductImageInputError as e:
        return _error(str(e), 400, usage)
    except Exception as e:
        logger.exception(f"[ProductImage HeldAuto] Lỗi ghép SP: {e}")
        return _error(f'Lỗi ghép sản phẩm: {e}', 500, usage)

    logger.info(
        f"[ProductImage HeldAuto] {len(results)}/{len(poses)} tư thế, túi {width_cm or '?'}cm, sau {time.time() - t0:.1f}s: "
        f"{[(info['pose_id'], info['score']) for _, info in results]}"
    )
    return Response({
        'success': True,
        'usage': usage,
        'candidates': [
            {'image_base64': base64.b64encode(image_bytes).decode('ascii'), 'mime_type': 'image/jpeg', 'info': info}
            for image_bytes, info in results
        ],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def scene_placement(request):
    """
    Body (multipart, hoặc JSON base64 kiểu cũ): {
      "product_image_base64": "...", "product_mime_type": "image/jpeg",  // SP, có hoặc không có nền
      "scene_image_base64": "...", "scene_mime_type": "image/jpeg",      // ảnh cảnh giữ nguyên
      "note": "túi cao khoảng 25cm"                                      // không bắt buộc
    }
    Response: giống held-product — { "success": true, "image_base64", "mime_type", "usage" }.
    """
    try:
        product_bytes, product_mime = _input_image(request, 'product_image', 'product_mime_type', 'ảnh sản phẩm')
        scene_bytes, scene_mime = _input_image(request, 'scene_image', 'scene_mime_type', 'ảnh cảnh')
        prompt = svc.build_scene_placement_prompt(request.data.get('note'))
        model_name = svc.read_gemini_image_model()
    except svc.ProductImageInputError as e:
        return _error(str(e), 400, cost.usage_not_billed(None, 'Đầu vào sai, chưa gọi Gemini — không tính phí.'))
    except svc.ProductImageConfigError as e:
        logger.error(f"[ProductImage ScenePlacement] Cấu hình: {e}")
        return _error(str(e), 500, cost.usage_not_billed(None, 'Thiếu cấu hình, chưa gọi Gemini — không tính phí.'))

    # Thứ tự là contract với prompt: "image 1" là SP, "image 2" là cảnh.
    return _generate_image(
        'ScenePlacement',
        [{'mime_type': product_mime, 'data': product_bytes}, {'mime_type': scene_mime, 'data': scene_bytes}],
        prompt,
        model_name,
    )


def _generate_image(tag, images, prompt, model_name):
    """Một lượt Gemini tạo ảnh từ `images` (theo đúng thứ tự prompt gọi tên) + prompt, dùng chung cho
    các chế độ tính phí. Mọi phản hồi từ lúc đã chạm tới Gemini đều kèm `usage` để BE ghi chi phí."""
    log = f"[ProductImage {tag}]"
    api_key = str(getattr(settings, 'GEMINI_API_KEY', '') or '').strip()
    if not api_key:
        return _error(
            'GEMINI_API_KEY chưa được set trên AI Service.',
            500,
            cost.usage_not_billed(model_name, 'Thiếu cấu hình, chưa gọi Gemini — không tính phí.'),
        )

    import google.generativeai as genai
    from google.api_core import exceptions as google_api_exceptions

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    t0 = time.time()
    sizes = ' + '.join(f"{len(image['data']) / 1024:.0f}KB" for image in images)
    logger.info(f"{log} Gọi Gemini model={model_name}, ảnh {sizes}")
    try:
        response = model.generate_content(
            [*images, prompt],
            # Xin cả TEXT lẫn IMAGE — chữ đi kèm là manh mối khi không có ảnh.
            generation_config={'response_modalities': ['TEXT', 'IMAGE']},
            request_options={'timeout': HELD_PRODUCT_TIMEOUT_SECONDS},
        )
    except (google_api_exceptions.DeadlineExceeded, google_api_exceptions.RetryError) as e:
        logger.error(f"{log} Timeout sau {time.time() - t0:.1f}s: {e}")
        return _error(
            f'AI tạo ảnh quá lâu (>{HELD_PRODUCT_TIMEOUT_SECONDS}s), vui lòng thử lại.',
            504,
            cost.usage_unknown(
                model_name, 'Hết thời gian chờ — không biết Gemini đã xử lý (và tính phí) lượt này chưa.'
            ),
        )
    except google_api_exceptions.ResourceExhausted as e:
        logger.error(f"{log} Vượt quota Gemini: {e}")
        return _error(
            'Gemini đã hết hạn mức (quota). Thử lại sau hoặc nhờ quản trị viên kiểm tra billing.',
            503,
            cost.usage_not_billed(model_name, 'Gemini từ chối vì hết hạn mức — không tính phí.'),
        )
    except google_api_exceptions.NotFound as e:
        # Tên model sai/đã bị Google ngừng — báo sửa biến, không tự đổi sang model khác.
        logger.error(f"{log} Model không tồn tại: {e}")
        return _error(
            f'{svc.GEMINI_MODEL_ENV}={model_name} không dùng được (Gemini trả 404) — sửa biến này '
            f'trên AI Service.',
            500,
            cost.usage_not_billed(model_name, 'Gemini không tìm thấy model — không tính phí.'),
        )
    except google_api_exceptions.GoogleAPICallError as e:
        logger.error(f"{log} Lỗi Gemini API: {e}")
        return _error(
            f'Lỗi gọi Gemini API: {e}',
            502,
            cost.usage_not_billed(model_name, 'Gemini báo lỗi, không trả kết quả — không tính phí.'),
        )
    except Exception as e:
        logger.exception(f"{log} Lỗi không xác định: {e}")
        return _error(f'Lỗi hệ thống: {e}', 500)

    image_bytes, image_mime, texts, finish_reason = svc.extract_generated_image(response)
    elapsed = time.time() - t0
    # Gemini đã chạy xong lượt này — có ảnh hay không đều đã tốn token.
    usage = cost.usage_from_response(model_name, response, texts)
    if image_bytes and not image_mime:
        logger.error(f"{log} Gemini trả dữ liệu ảnh nhưng không nhận ra định dạng")
        return _error('Gemini trả về dữ liệu ảnh không đọc được, vui lòng thử lại.', 502, usage)
    if not image_bytes:
        logger.error(
            f"{log} Gemini không trả ảnh sau {elapsed:.1f}s. "
            f"finish_reason={finish_reason}, text={texts[:1]}"
        )
        # Không đoán lý do — đưa nguyên lời model nói.
        detail = f' Gemini trả lời: "{texts[0][:300]}"' if texts else ''
        return _error(
            f'Gemini không trả về ảnh (finish_reason={finish_reason}).{detail} Thử lại, đổi ảnh '
            f'khác, hoặc kiểm tra {svc.GEMINI_MODEL_ENV}={model_name} có phải model tạo ảnh.',
            502,
            usage,
        )

    logger.info(
        f"{log} ✅ {image_mime} {len(image_bytes) / 1024:.0f}KB sau {elapsed:.1f}s, "
        f"token vào/ra={usage['input_tokens']}/{usage['output_tokens']} cost_usd={usage['cost_usd']}"
    )
    return Response({
        'success': True,
        'image_base64': base64.b64encode(image_bytes).decode('ascii'),
        'mime_type': image_mime,
        'usage': usage,
    })
