"""
Views for scraped-video script analysis (dịch + phân tích 1 video cào từ scraper
subsystem). Chỉ chịu trách nhiệm gọi model AI và trả kết quả — BE tự lưu vào
approved_content sau khi duyệt.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from video_management.services.scraped_video_script_service import analyze_scraped_video

logger = logging.getLogger(__name__)


@api_view(["POST"])
def generate_scraped_video_script(request):
    """
    POST /api/scraped-video/script/generate/
    Body:
    {
        "platform": "...",
        "title": "...",
        "description": "...",
        "hashtags": [str],        // optional
        "views_count": int,       // optional
        "likes_count": int,       // optional
        "comments_count": int,    // optional
    }
    Response: { "vietnamese_content": str, "script_outline": str, "hashtags": [str] }
    """
    try:
        params = {
            "platform": request.data.get("platform"),
            "title": request.data.get("title"),
            "description": request.data.get("description"),
            "hashtags": request.data.get("hashtags") or [],
            "views_count": request.data.get("views_count"),
            "likes_count": request.data.get("likes_count"),
            "comments_count": request.data.get("comments_count"),
        }

        result = analyze_scraped_video(params)
        return Response(result, status=status.HTTP_200_OK)

    except ValueError as e:
        logger.warning(f"Scraped video script generation failed (bad request): {e}")
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error generating scraped video script: {e}", exc_info=True)
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
