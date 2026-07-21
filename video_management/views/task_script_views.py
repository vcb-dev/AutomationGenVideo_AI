"""
Views for task-auto video script generation (adapt content thắng cho sản phẩm mới).
Chỉ chịu trách nhiệm gọi model AI và trả kết quả — BE tự lo caching/lưu DB.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from video_management.services.task_script_service import (
    generate_video_script,
    translate_video_script,
)

logger = logging.getLogger(__name__)


@api_view(["POST"])
def generate_task_video_script(request):
    """
    POST /api/task-auto/video-script/generate/
    Body:
    {
        "fileUrl": "...",            // optional
        "scriptText": "...",         // optional
        "contentTitle": "...",       // optional
        "contentLine": "A1..A5",     // optional
        "contentMarket": "...",      // optional
        "productName": "...",
        "productSku": "...",
        "productPrice": "...",
        "productMaterial": "...",
        "productPriceSegment": "...",
        "productLine": "...",
        "productMarket": "...",
    }
    Response: { "content": str, "hashtags": [str], "translation": {...} | null }
    """
    try:
        params = {
            "fileUrl": request.data.get("fileUrl"),
            "scriptText": request.data.get("scriptText"),
            "contentTitle": request.data.get("contentTitle"),
            "contentLine": request.data.get("contentLine"),
            "contentMarket": request.data.get("contentMarket"),
            "productName": request.data.get("productName"),
            "productSku": request.data.get("productSku"),
            "productPrice": request.data.get("productPrice"),
            "productMaterial": request.data.get("productMaterial"),
            "productPriceSegment": request.data.get("productPriceSegment"),
            "productLine": request.data.get("productLine"),
            "productMarket": request.data.get("productMarket"),
        }

        result = generate_video_script(params)
        return Response(result, status=status.HTTP_200_OK)

    except ValueError as e:
        logger.warning(f"Task video script generation failed (bad request): {e}")
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error generating task video script: {e}", exc_info=True)
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
def translate_task_video_script(request):
    """
    POST /api/task-auto/video-script/translate/
    Dịch lại content/hashtags hiện có (vd sau khi user sửa tay), không đọc lại file nguồn,
    không sinh script mới — chỉ dịch. Cần truyền `language` (đã biết trước, vd dịch lại) HOẶC
    `market` (lần dịch đầu tiên, chưa biết ngôn ngữ — AI tự xác định dựa trên thị trường).
    Body:
    {
        "content": "...",       // required
        "hashtags": [str],      // optional
        "language": "...",      // optional nếu có market — vd 'Tiếng Anh (Mỹ)', 'Tiếng Thái'
        "market": "..."         // optional nếu có language — vd 'Indonesia'
    }
    Response: { "language": str, "content": str, "hashtags": [str] }
    """
    try:
        content = request.data.get("content")
        language = request.data.get("language")
        market = request.data.get("market")
        hashtags = request.data.get("hashtags") or []

        if not content:
            return Response({"error": "Thiếu content cần dịch"}, status=status.HTTP_400_BAD_REQUEST)
        if not language and not market:
            return Response({"error": "Thiếu language hoặc market cần dịch sang"}, status=status.HTTP_400_BAD_REQUEST)

        result = translate_video_script(content, hashtags, language=language, market=market)
        return Response(result, status=status.HTTP_200_OK)

    except ValueError as e:
        logger.warning(f"Task video script translation failed (bad request): {e}")
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error translating task video script: {e}", exc_info=True)
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
