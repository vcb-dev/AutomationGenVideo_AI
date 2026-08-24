"""
ID Photo — ghép trang phục ảnh thẻ nhân viên bằng Gemini image editing.

Vô trạng thái, không có model/DB riêng cho tính năng này — đúng kiến trúc "AI service chỉ
xử lý, BE orchestrate + lưu lịch sử" đã dùng cho transcribe_upload (transcribe_views.py) và
transform_content (content_generation_views.py). BE gửi thẳng ảnh dạng base64 inline (đã có
sẵn trong bộ nhớ tạm của BE từ bước upload trước đó qua uploadId) — KHÔNG dùng Gemini Files
API (không cần polling): ảnh giới hạn 10MB, nằm gọn trong giới hạn inline data của
generateContent, nên gọi 1 lượt duy nhất là đủ, tránh hẳn lớp phức tạp polling + 2 timeout
lồng nhau mà transcribe_with_gemini() phải xử lý (GEMINI_FILE_PROCESSING_TIMEOUT + phần ngân
sách còn lại cho generate_content) cho trường hợp Files API.
"""
import os
import time
import base64
import logging

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

logger = logging.getLogger(__name__)

# Model image-editing của Gemini ("nano banana") — nhận ảnh + prompt text, trả về ảnh đã sửa
# trong cùng 1 lượt generateContent (không cần Files API/polling vì input chỉ vài MB, xa dưới
# giới hạn inline data của API). Xác nhận model này khả dụng với API key hiện tại qua
# genai.list_models() (hỗ trợ generateContent) trước khi chọn làm default.
DEFAULT_GEMINI_IMAGE_MODEL = 'gemini-2.5-flash-image'

# BE cam kết timeout 90s cho lượt gọi này (id-photo.service.ts#MERGE_OUTFIT_TIMEOUT_MS). Đặt
# ngân sách nội bộ thấp hơn hẳn để AI service LUÔN kịp trả lỗi rõ ràng về trước khi bị BE tự
# timeout ở giữa chừng (cùng bài học với ngân sách của transcribe_with_gemini) — không
# có giai đoạn polling nào ăn vào ngân sách này (khác transcribe), toàn bộ 75s dành hết cho 1
# lệnh gọi generate_content duy nhất.
MERGE_OUTFIT_TIMEOUT_SECONDS = 75

# Chặn sớm ảnh input bất thường lớn — phòng hờ dù BE đã validate 10MB ở bước upload, không tin
# tưởng tuyệt đối input từ bên gọi khác (defense in depth).
MAX_INPUT_IMAGE_MB = 12

ALLOWED_INPUT_MIME_TYPES = {'image/jpeg', 'image/jpg', 'image/png'}

# Prompt đã chốt — KHÔNG đổi nội dung ngoài khối OUTFIT.
# Khối OUTFIT cập nhật theo đồng phục thật trên thẻ nhân viên công ty: vest cổ tim đen khoác
# ngoài sơ mi trắng + cà vạt xám (bản trước mô tả áo polo đen, không khớp mẫu thẻ thật).
# Các khối còn lại (giữ khuôn mặt, tư thế, bố cục, nền, chất lượng, loại trừ) giữ nguyên.
MERGE_OUTFIT_PROMPT = (
    "Keep 100% of the original face, facial structure, expression, eyes, skin tone, "
    "hairstyle, and facial lighting exactly as in the original image. Absolutely do not "
    "modify the face, do not recreate the face, do not blur, and do not alter any facial "
    "details in any way. ONLY CHANGE THE OUTFIT TO MATCH THE REFERENCE AND MAKE IT AN ID "
    "PHOTO.\n"
    "OUTFIT: Black V-neck vest (sleeveless sweater vest) worn over a white collared dress "
    "shirt. Shirt collar visible, folded neatly over the vest neckline. Gray or silver "
    "necktie, neatly knotted, visible at the collar. Vest has a thin decorative trim (light "
    "blue or white piping) along the V-neck edge. Clean, smooth fabric with natural texture, "
    "neatly fitted and wrinkle-free. Overall appearance formal, neat, and professional — "
    "school/corporate uniform style. No additional accessories or patterns beyond described.\n"
    "POSE: Straight upright posture. Shoulders relaxed and balanced. Arms naturally lowered. "
    "Neutral, formal ID photo expression. Body proportions unchanged, no stretching or "
    "distortion.\n"
    "COMPOSITION & CAMERA: Expand the framing to a waist-up portrait, including the subject "
    "from the waist upward while keeping the subject centered and maintaining natural body "
    "proportions. Front-facing angle. Camera positioned at eye level. Subject centered "
    "perfectly in the frame.\n"
    "BACKGROUND & LIGHTING: Replace the background with a solid pure white background "
    "(#FFFFFF). Background must be completely uniform, flat color. No gradients, no texture, "
    "no shadows, no vignetting, no lighting effects. Subject must remain naturally separated "
    "from the background. Do not alter facial lighting while changing the background.\n"
    "IMAGE QUALITY: Ultra high resolution. Sharp focus across entire subject. Natural skin "
    "texture, no artificial smoothing. Neutral color tones, balanced contrast. Fully "
    "photorealistic. Must not look AI-generated.\n"
    "EXCLUSIONS: Do not change the face. Do not change facial lighting. Do not recreate or "
    "retouch the face in any form. No cartoon, painting, CGI style. No AI-like appearance."
)


@api_view(['POST'])
# BE đã xác thực JWT của người dùng thật ở tầng trước (JwtAuthGuard + RolesGuard trên
# IdPhotoController) rồi mới gọi sang đây server-to-server — cùng cách transform_content
# (content_generation_views.py) đang để mặc định AllowAny của REST_FRAMEWORK. Route này không
# dành cho FE gọi thẳng.
@permission_classes([AllowAny])
def merge_outfit(request):
    """
    POST /api/ai/id-photo/merge-outfit/
    Body (JSON): { "image_base64": "...", "mime_type": "image/jpeg" }
    Response 200: { "success": true, "processed_image_base64": "..." }
    Response lỗi: { "success": false, "error_message": "..." }
    """
    image_base64 = request.data.get('image_base64')
    mime_type = (request.data.get('mime_type') or '').strip().lower()

    if not image_base64:
        return Response({'success': False, 'error_message': 'image_base64 is required'}, status=400)
    if not mime_type:
        return Response({'success': False, 'error_message': 'mime_type is required'}, status=400)
    if mime_type not in ALLOWED_INPUT_MIME_TYPES:
        return Response({
            'success': False,
            'error_message': f'mime_type không được hỗ trợ: {mime_type}. Chỉ chấp nhận JPG/PNG.',
        }, status=400)

    try:
        # validate=True: từ chối ngay nếu chuỗi không phải base64 hợp lệ, thay vì âm thầm decode
        # sai rồi gửi rác lên Gemini và nhận lỗi mù mờ ở tầng xa hơn.
        image_bytes = base64.b64decode(image_base64, validate=True)
    except Exception:
        return Response({'success': False, 'error_message': 'image_base64 không hợp lệ (không giải mã được).'}, status=400)

    if not image_bytes:
        return Response({'success': False, 'error_message': 'image_base64 giải mã ra rỗng.'}, status=400)

    image_size_mb = len(image_bytes) / (1024 * 1024)
    if image_size_mb > MAX_INPUT_IMAGE_MB:
        return Response({
            'success': False,
            'error_message': f'Ảnh quá lớn ({image_size_mb:.1f}MB > {MAX_INPUT_IMAGE_MB}MB).',
        }, status=400)

    api_key = str(getattr(settings, 'GEMINI_API_KEY', '')).strip() or os.getenv('GEMINI_API_KEY', '').strip()
    if not api_key:
        return Response({'success': False, 'error_message': 'Hệ thống chưa cấu hình GEMINI_API_KEY trên AI Service.'}, status=500)

    model_name = str(
        getattr(settings, 'GEMINI_IMAGE_MODEL', None) or os.getenv('GEMINI_IMAGE_MODEL', DEFAULT_GEMINI_IMAGE_MODEL)
    ).strip()

    import google.generativeai as genai
    from google.api_core import exceptions as google_api_exceptions

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)
    image_part = {'mime_type': mime_type, 'data': image_bytes}

    t0 = time.time()
    logger.info(f"[IdPhoto MergeOutfit] Gọi Gemini model={model_name}, input={image_size_mb:.2f}MB, timeout={MERGE_OUTFIT_TIMEOUT_SECONDS}s")
    try:
        response = model.generate_content(
            [image_part, MERGE_OUTFIT_PROMPT],
            # Cho phép cả TEXT lẫn IMAGE trong response: model có thể kèm 1 đoạn text ngắn
            # (caption/giải thích) trước phần ảnh — chỉ xin IMAGE có nguy cơ model trả lỗi ở
            # vài phiên bản API. Code parse response bên dưới bỏ qua phần text, chỉ lấy ảnh.
            generation_config={'response_modalities': ['TEXT', 'IMAGE']},
            request_options={'timeout': MERGE_OUTFIT_TIMEOUT_SECONDS},
        )
    except (google_api_exceptions.DeadlineExceeded, google_api_exceptions.RetryError) as e:
        elapsed = time.time() - t0
        logger.error(f"[IdPhoto MergeOutfit] Timeout sau {elapsed:.1f}s (ngân sách {MERGE_OUTFIT_TIMEOUT_SECONDS}s): {e}")
        return Response({
            'success': False,
            'error_message': f'AI xử lý ảnh quá lâu (>{MERGE_OUTFIT_TIMEOUT_SECONDS}s), vui lòng thử lại.',
        }, status=504)
    except google_api_exceptions.ResourceExhausted as e:
        logger.error(f"[IdPhoto MergeOutfit] Vượt quota Gemini: {e}")
        return Response({
            'success': False,
            'error_message': 'AI service đã vượt hạn mức sử dụng Gemini (quota). Vui lòng thử lại sau hoặc liên hệ quản trị viên để kiểm tra billing.',
        }, status=503)
    except google_api_exceptions.GoogleAPICallError as e:
        logger.error(f"[IdPhoto MergeOutfit] Lỗi Gemini API: {e}")
        return Response({'success': False, 'error_message': f'Lỗi gọi Gemini API: {str(e)}'}, status=502)
    except Exception as e:
        logger.exception(f"[IdPhoto MergeOutfit] Lỗi không xác định: {e}")
        return Response({'success': False, 'error_message': f'Lỗi hệ thống: {str(e)}'}, status=500)

    elapsed = time.time() - t0
    logger.info(f"[IdPhoto MergeOutfit] Gemini trả lời sau {elapsed:.1f}s")

    processed_bytes = None
    candidates = getattr(response, 'candidates', None) or []
    if candidates:
        parts = getattr(candidates[0].content, 'parts', None) or []
        for part in parts:
            inline = getattr(part, 'inline_data', None)
            # inline_data.data là bytes thô (proto BYTES field) — không phải chuỗi base64,
            # xem google.ai.generativelanguage_v1beta.types.content.Blob.
            if inline and inline.data:
                processed_bytes = inline.data
                break

    if not processed_bytes:
        finish_reason = getattr(candidates[0], 'finish_reason', None) if candidates else None
        logger.error(f"[IdPhoto MergeOutfit] Gemini không trả ảnh. finish_reason={finish_reason}, candidates={len(candidates)}")
        return Response({
            'success': False,
            'error_message': (
                'Gemini không trả về ảnh đã ghép áo — có thể ảnh gốc bị bộ lọc an toàn chặn '
                'hoặc không nhận diện được khuôn mặt rõ ràng. Vui lòng thử ảnh khác.'
            ),
        }, status=502)

    logger.info(f"[IdPhoto MergeOutfit] ✅ Thành công, output={len(processed_bytes) / 1024:.1f}KB")
    return Response({
        'success': True,
        # Luôn PNG — model gemini-2.5-flash-image trả ảnh sinh ra dạng PNG, và contract với BE
        # (id-photo.service.ts) không có field mime_type riêng cho output, cố định để 2 bên
        # khỏi lệch giả định.
        'processed_image_base64': base64.b64encode(processed_bytes).decode('ascii'),
    })
