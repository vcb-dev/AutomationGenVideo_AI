"""
Tạo ảnh sản phẩm mới — 2 endpoint cho 2 chế độ, xem mô tả đầy đủ ở
video_management/services/product_image_service.py.

- POST /api/ai/product-image/cutout/        — rembg tách nền ảnh SP (0đ)
- POST /api/ai/product-image/held-product/  — Gemini thay SP trên tay chị Nhạm (tính phí)

Cả hai bắt buộc JWT (IsAuthenticated): BE ký token theo đúng người bấm rồi gọi server-to-server.
Khác ảnh thẻ đang để AllowAny — URL AI service trên Railway gọi thẳng được từ ngoài, để trống
thì ai biết địa chỉ cũng đốt được tiền Gemini qua endpoint held-product.
"""
import base64
import logging
import time

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from video_management.services import product_image_cost as cost
from video_management.services import product_image_service as svc

logger = logging.getLogger(__name__)

# BE chờ lượt Gemini tối đa 90s (product-image.service.ts#HELD_PRODUCT_TIMEOUT_MS). Ngân sách
# bên này thấp hơn hẳn để AI service luôn kịp trả lỗi rõ ràng trước khi BE tự huỷ — cùng mốc với
# id_photo_views.MERGE_OUTFIT_TIMEOUT_SECONDS.
HELD_PRODUCT_TIMEOUT_SECONDS = 75


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
    Body (JSON): { "image_base64": "...", "mime_type": "image/jpeg" }
    Response 200: { "success": true, "cutout_image_base64": "<PNG>", "width": 812, "height": 1240 }
    """
    try:
        image_bytes, _ = svc.decode_input_image(
            request.data.get('image_base64'), request.data.get('mime_type'), 'ảnh sản phẩm'
        )
        model_name = svc.read_rembg_model()
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
    try:
        png_bytes, width, height = svc.cut_out_product(
            image_bytes, lambda image: remove(image, session=session)
        )
    except svc.ProductImageInputError as e:
        return _error(str(e), 400)
    except Exception as e:
        logger.exception(f"[ProductImage Cutout] Lỗi tách nền: {e}")
        return _error(f'Lỗi tách nền: {e}', 500)

    logger.info(
        f"[ProductImage Cutout] model={model_name} {width}x{height} "
        f"{len(png_bytes) / 1024:.0f}KB sau {time.time() - t0:.1f}s"
    )
    return Response({
        'success': True,
        'cutout_image_base64': base64.b64encode(png_bytes).decode('ascii'),
        'width': width,
        'height': height,
        'model': model_name,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def held_product(request):
    """
    Body (JSON): {
      "person_image_base64": "...", "person_mime_type": "image/jpeg",   // chị Nhạm cầm SP cũ
      "product_image_base64": "...", "product_mime_type": "image/png",  // SP mới
      "note": "hộp cao khoảng 30cm"                                     // không bắt buộc
    }
    Response 200: { "success": true, "image_base64": "...", "mime_type": "image/png", "usage": {...} }
    `usage` (có cả trong phản hồi lỗi khi đã gọi tới Gemini) — xem product_image_cost.py:
      { "model", "input_tokens", "output_tokens", "cost_usd": số | null, "cost_note": lý do | null }
    """
    try:
        person_bytes, person_mime = svc.decode_input_image(
            request.data.get('person_image_base64'),
            request.data.get('person_mime_type'),
            'ảnh chị Nhạm cầm sản phẩm',
        )
        product_bytes, product_mime = svc.decode_input_image(
            request.data.get('product_image_base64'),
            request.data.get('product_mime_type'),
            'ảnh sản phẩm mới',
        )
        prompt = svc.build_held_product_prompt(request.data.get('note'))
        model_name = svc.read_gemini_image_model()
    except svc.ProductImageInputError as e:
        return _error(str(e), 400, cost.usage_not_billed(None, 'Đầu vào sai, chưa gọi Gemini — không tính phí.'))
    except svc.ProductImageConfigError as e:
        logger.error(f"[ProductImage HeldProduct] Cấu hình: {e}")
        return _error(str(e), 500, cost.usage_not_billed(None, 'Thiếu cấu hình, chưa gọi Gemini — không tính phí.'))

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
    logger.info(
        f"[ProductImage HeldProduct] Gọi Gemini model={model_name}, "
        f"person={len(person_bytes) / 1024:.0f}KB product={len(product_bytes) / 1024:.0f}KB"
    )
    try:
        response = model.generate_content(
            [
                {'mime_type': person_mime, 'data': person_bytes},
                {'mime_type': product_mime, 'data': product_bytes},
                prompt,
            ],
            # Xin cả TEXT lẫn IMAGE như ảnh thẻ: chỉ xin IMAGE thì vài phiên bản API báo lỗi;
            # phần chữ đi kèm còn là manh mối khi model không trả ảnh.
            generation_config={'response_modalities': ['TEXT', 'IMAGE']},
            request_options={'timeout': HELD_PRODUCT_TIMEOUT_SECONDS},
        )
    except (google_api_exceptions.DeadlineExceeded, google_api_exceptions.RetryError) as e:
        logger.error(f"[ProductImage HeldProduct] Timeout sau {time.time() - t0:.1f}s: {e}")
        return _error(
            f'AI tạo ảnh quá lâu (>{HELD_PRODUCT_TIMEOUT_SECONDS}s), vui lòng thử lại.',
            504,
            cost.usage_unknown(
                model_name, 'Hết thời gian chờ — không biết Gemini đã xử lý (và tính phí) lượt này chưa.'
            ),
        )
    except google_api_exceptions.ResourceExhausted as e:
        logger.error(f"[ProductImage HeldProduct] Vượt quota Gemini: {e}")
        return _error(
            'Gemini đã hết hạn mức (quota). Thử lại sau hoặc nhờ quản trị viên kiểm tra billing.',
            503,
            cost.usage_not_billed(model_name, 'Gemini từ chối vì hết hạn mức — không tính phí.'),
        )
    except google_api_exceptions.NotFound as e:
        # Tên model sai/đã bị Google ngừng — báo sửa biến, không tự đổi sang model khác.
        logger.error(f"[ProductImage HeldProduct] Model không tồn tại: {e}")
        return _error(
            f'{svc.GEMINI_MODEL_ENV}={model_name} không dùng được (Gemini trả 404) — sửa biến này '
            f'trên AI Service.',
            500,
            cost.usage_not_billed(model_name, 'Gemini không tìm thấy model — không tính phí.'),
        )
    except google_api_exceptions.GoogleAPICallError as e:
        logger.error(f"[ProductImage HeldProduct] Lỗi Gemini API: {e}")
        return _error(
            f'Lỗi gọi Gemini API: {e}',
            502,
            cost.usage_not_billed(model_name, 'Gemini báo lỗi, không trả kết quả — không tính phí.'),
        )
    except Exception as e:
        logger.exception(f"[ProductImage HeldProduct] Lỗi không xác định: {e}")
        return _error(f'Lỗi hệ thống: {e}', 500)

    image_bytes, image_mime, texts, finish_reason = svc.extract_generated_image(response)
    elapsed = time.time() - t0
    # Gemini đã chạy xong lượt này — có ảnh hay không đều đã tốn token.
    usage = cost.usage_from_response(model_name, response, texts)
    if image_bytes and not image_mime:
        logger.error("[ProductImage HeldProduct] Gemini trả dữ liệu ảnh nhưng không nhận ra định dạng")
        return _error('Gemini trả về dữ liệu ảnh không đọc được, vui lòng thử lại.', 502, usage)
    if not image_bytes:
        logger.error(
            f"[ProductImage HeldProduct] Gemini không trả ảnh sau {elapsed:.1f}s. "
            f"finish_reason={finish_reason}, text={texts[:1]}"
        )
        # Không đoán lý do: model chỉ ra chữ, bộ lọc an toàn, hay ảnh khó đều cho cùng triệu
        # chứng — đưa nguyên lời model nói (nếu có) để người dùng/quản trị tự phán đoán.
        detail = f' Gemini trả lời: "{texts[0][:300]}"' if texts else ''
        return _error(
            f'Gemini không trả về ảnh (finish_reason={finish_reason}).{detail} Thử lại, đổi ảnh '
            f'khác, hoặc kiểm tra {svc.GEMINI_MODEL_ENV}={model_name} có phải model tạo ảnh.',
            502,
            usage,
        )

    logger.info(
        f"[ProductImage HeldProduct] ✅ {image_mime} {len(image_bytes) / 1024:.0f}KB sau {elapsed:.1f}s, "
        f"token vào/ra={usage['input_tokens']}/{usage['output_tokens']} cost_usd={usage['cost_usd']}"
    )
    return Response({
        'success': True,
        'image_base64': base64.b64encode(image_bytes).decode('ascii'),
        'mime_type': image_mime,
        'usage': usage,
    })
