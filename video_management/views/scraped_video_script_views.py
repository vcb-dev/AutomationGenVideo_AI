"""
Views for scraped-video script analysis (dịch + phân tích 1 video cào từ scraper
subsystem). Chỉ chịu trách nhiệm gọi model AI và trả kết quả — BE tự lưu vào
approved_content sau khi duyệt.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from video_management.services import usage_meter
from video_management.services import video_script_pipeline as pipeline
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


class VideoScriptThrottle(SimpleRateThrottle):
    """Mỗi lượt có thể tốn 1 lệnh gọi TikHub + 1 lệnh gọi Gemini (tính phí) và ghi video ra
    đĩa — chặn theo người gọi (BE ký token nội bộ theo leader duyệt)."""
    scope = 'video_script'

    def get_cache_key(self, request, view):
        ident = getattr(request.user, 'pk', None) or self.get_ident(request)
        return self.cache_format % {'scope': self.scope, 'ident': ident}


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([VideoScriptThrottle])
def generate_script_from_video(request):
    """
    POST /api/scraped-video/script-from-video/
    Body: { "video_url": str, "platform": str?, "video_id": str?, "title": str?, "description": str? }

    Tải video (miễn phí trước, TikHub dự phòng) rồi Gemini viết kịch bản từ lời thoại + hình.
    Response 200:
      status=DONE             — có kịch bản: script_text, transcript, has_voice, language, source
      status=ENGINE_DISABLED  — engine chưa bật: KHÔNG tải video (không tốn TikHub), BE giữ cách viết
                                kịch bản cũ. `reason` nêu các biến bắt buộc còn thiếu
                                (GEMINI_API_KEY, VIDEO_TO_TEXT_GEMINI_MODEL, VIDEO_SCRIPT_MAX_SECONDS),
                                rỗng khi cố ý tắt (VIDEO_SCRIPT_ENGINE=off)
    `download` luôn có mặt: {ok, source, duration, has_audio, size_mb, trimmed, error}.
    `usage` luôn có mặt: chi phí TikHub/Gemini của lượt này (usage_meter) để BE thống kê.
    Lỗi engine → 502 {status: FAILED, error, download}.
    """
    video_url = str(request.data.get("video_url") or "").strip()
    if not video_url.startswith(("http://", "https://")):
        return Response({"status": "FAILED", "error": "Thiếu video_url hợp lệ."}, status=status.HTTP_400_BAD_REQUEST)
    platform = str(request.data.get("platform") or "").strip().lower()
    video_id = str(request.data.get("video_id") or "").strip()
    title = str(request.data.get("title") or "")
    description = str(request.data.get("description") or "")

    video = None
    with usage_meter.collect() as usage:
        try:
            response = _script_from_video(video_url, platform, video_id, title, description)
            video = response.pop('_video', None)
            code = response.pop('_code', status.HTTP_200_OK)
            # Chi phí TikHub/Gemini của lượt này — BE lưu để thống kê (kể cả khi thất bại).
            response['usage'] = list(usage)
            return Response(response, status=code)
        finally:
            pipeline.cleanup_video(video)


def _script_from_video(video_url, platform, video_id, title, description) -> dict:
    """Trả dict phản hồi kèm '_video' (file tạm để view dọn) và '_code' (mã HTTP nếu khác 200)."""
    if pipeline.script_engine() != "gemini":
        # Kiểm cấu hình TRƯỚC khi tải: engine tắt thì tải về cũng không dùng, chỉ tốn TikHub.
        reason = pipeline.engine_disabled_reason()
        logger.info(f"[VideoScript] engine tắt ({reason or 'VIDEO_SCRIPT_ENGINE=off'}) — không tải {video_url[:80]}")
        return {"status": "ENGINE_DISABLED", "reason": reason,
                "download": {"ok": False, "error": "Không tải vì chưa bật viết kịch bản từ video."}}
    video = None
    try:
        video = pipeline.download_for_analysis(video_url, platform=platform, video_id=video_id)
        download = {"ok": True, **video.as_dict(), "error": None}
    except pipeline.VideoDownloadError as e:
        download = {"ok": False, "error": str(e)}
    try:
        return _script_for(video, download, video_url, platform, title, description)
    except BaseException:
        pipeline.cleanup_video(video)  # lỗi bất ngờ sau khi đã tải: không để sót file tạm
        raise


def _script_for(video, download, video_url, platform, title, description) -> dict:
    logger.info(f"[VideoScript] {platform or pipeline.detect_platform(video_url)} {video_url[:80]} → tải: {download}")

    from video_management.services import gemini_video_script
    try:
        result = gemini_video_script.generate_script(
            video.path if video else None, title, description,
            audio_seconds=video.duration if video and video.has_audio else 0,
        )
    except gemini_video_script.GeminiQuotaExceeded as e:
        logger.warning(f"[VideoScript] {e}")
        return {"status": "FAILED", "error": str(e), "download": download, "_video": video, "_code": status.HTTP_502_BAD_GATEWAY}
    except Exception as e:  # noqa: BLE001
        logger.error(f"[VideoScript] Gemini lỗi: {e}", exc_info=True)
        return {"status": "FAILED", "error": f"Gemini: {str(e)[:300]}", "download": download, "_video": video, "_code": status.HTTP_502_BAD_GATEWAY}
    if not result.get("script_text"):
        return {"status": "FAILED", "error": "Gemini không trả kịch bản.", "download": download, "_video": video, "_code": status.HTTP_502_BAD_GATEWAY}
    return {"status": "DONE", **result, "download": download, "_video": video}
