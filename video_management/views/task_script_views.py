"""
Views for task-auto video script generation (adapt content thắng cho sản phẩm mới).
Chỉ chịu trách nhiệm gọi model AI và trả kết quả — BE tự lo caching/lưu DB.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from video_management.services.task_script_service import generate_video_script

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
