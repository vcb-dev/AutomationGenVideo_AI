"""
Views cho PAAST Content Analyzer.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from video_management.services.paast_analysis_service import PaastAnalysisService, PaastAnalysisServiceV2

logger = logging.getLogger(__name__)

MIN_CONTENT_LENGTH = 100

# KHÔNG giới hạn độ dài trên. Trước đây chặn cứng 3000 ký tự, trong khi bước viết kịch bản của
# content-transform chạy với max_tokens=16000 nên output vượt 3000 ký tự là chuyện thường —
# endpoint /rescore truyền thẳng output_text vào đây và ăn 400. Tệ hơn: 400 là lỗi TẤT ĐỊNH
# nhưng BE vẫn retry đủ 3 lượt rồi thay message thật bằng "có thể do timeout", nên triệu chứng
# hiện ra là "chấm điểm timeout" trong khi thực chất là content quá dài.


def _read_timeout_seconds(request):
    """
    Timeout (giây) mà BE cho phép cho CẢ request này — BE gửi kèm để Django dùng ĐÚNG ngân sách
    bên gọi thực sự chờ, thay vì tự cắt theo hằng số hard-code riêng.

    Trả None khi thiếu/không hợp lệ ⇒ service rơi về DEFAULT_ANALYZE_TIMEOUT_S.
    """
    raw = request.data.get('timeout_seconds')
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"timeout_seconds không hợp lệ ({raw!r}) — dùng mặc định")
        return None
    return value if value > 0 else None


@api_view(['POST'])
def analyze_content(request):
    """
    Phân tích content theo khung PAAST (5 lớp x 6 tiêu chí) và tính điểm 0-100.

    POST /api/ai/paast/analyze/
    Body:
    {
        "content": "..."
    }
    """
    try:
        content = (request.data.get('content') or '').strip()

        if len(content) < MIN_CONTENT_LENGTH:
            return Response(
                {'error': f'Content quá ngắn — cần ít nhất {MIN_CONTENT_LENGTH} ký tự'},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = PaastAnalysisService()
        result = service.analyze(content, timeout_seconds=_read_timeout_seconds(request))

        return Response({
            'success': True,
            **result,
        })

    except RuntimeError as e:
        logger.error(f"PAAST analyze failed: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
    except Exception as e:
        logger.error(f"Error in analyze_content view: {str(e)}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
def upgrade_content(request):
    """
    Nâng cấp content dựa trên danh sách tiêu chí đang thiếu, rồi phân tích lại bản mới.

    POST /api/ai/paast/upgrade/
    Body:
    {
        "original_content": "...",
        "missing_elements": [
            {"layer": "acknowledge", "criterion": "STORY", "suggestion": "..."}
        ]
    }
    """
    try:
        original_content = (request.data.get('original_content') or '').strip()
        missing_elements = request.data.get('missing_elements') or []

        if len(original_content) < MIN_CONTENT_LENGTH:
            return Response(
                {'error': f'original_content quá ngắn — cần ít nhất {MIN_CONTENT_LENGTH} ký tự'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not isinstance(missing_elements, list):
            return Response(
                {'error': 'missing_elements phải là một mảng'},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = PaastAnalysisService()
        result = service.upgrade(
            original_content,
            missing_elements,
            timeout_seconds=_read_timeout_seconds(request),
        )

        return Response({
            'success': True,
            **result,
        })

    except RuntimeError as e:
        logger.error(f"PAAST upgrade failed: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
    except Exception as e:
        logger.error(f"Error in upgrade_content view: {str(e)}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
def analyze_content_v2(request):
    """
    Phân tích PAAST BẢN 2 — dùng cho video kênh nội bộ.

    POST /api/ai/paast/analyze-v2/
    Body: { "content": "..." }

    Khác bản 1: bỏ thang điểm 0–100 (chỉ đếm element + kết luận đạt/chưa), thêm 16 hook gợi ý
    trong `layers.action.hookSuggestions`, khoá JSON kiểu camelCase. Bản 1 giữ nguyên vì
    task-auto đang chạy trên nó — xem ghi chú ở PaastAnalysisServiceV2.
    """
    try:
        content = (request.data.get('content') or '').strip()

        if len(content) < MIN_CONTENT_LENGTH:
            return Response(
                {'error': f'Content quá ngắn — cần ít nhất {MIN_CONTENT_LENGTH} ký tự'},
                status=status.HTTP_400_BAD_REQUEST
            )
        result = PaastAnalysisServiceV2().analyze_v2(content)
        return Response({'success': True, **result})

    except Exception as e:
        logger.exception("[PAAST v2] Phân tích lỗi")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
