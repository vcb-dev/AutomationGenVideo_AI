"""
View lớp AI cho job khớp video kênh nội bộ ↔ task (BE task-video-match.service.ts).
Chỉ gọi model và trả phán quyết — BE tự kiểm lại và quyết định có gắn link hay không.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from video_management.services.task_video_match_judge_service import judge_cases

logger = logging.getLogger(__name__)


@api_view(["POST"])
def judge_task_video_match(request):
    """
    POST /api/task-auto/video-match/judge/
    Body:
    {
        "model": "deepseek-v4-flash" | "deepseek-v4-pro",   // optional
        "thinking": false,                                   // optional — bật suy luận (chậm hơn)
        "cases": [{
            "case_id": "FACEBOOK:<post_id>",
            "video": {"platform", "caption", "published_at", "transcript"?},
            "candidates": [{
                "task_id", "title", "script", "product_names", "product_skus",
                "anchor_at", "days_from_video", "has_platform_link", "linked_videos_same_platform"
            }]
        }]
    }
    Response: { "model": str, "results": [{case_id, task_id|null, confidence, reason} | {case_id, error}] }
    """
    try:
        result = judge_cases(
            request.data.get("cases"),
            request.data.get("model"),
            thinking=request.data.get("thinking") is True,
        )
        return Response(result, status=status.HTTP_200_OK)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error judging task video match: {e}", exc_info=True)
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
