"""Job nền + poll cho module content-transform (Chuyển đổi nội dung).

Vì sao cần: transcribe file dài và upgrade kịch bản dài chạy hàng trăm giây trong
MỘT request đồng bộ. Django neo ngân sách ~415s ngay khi vào view; khi Gemini/DeepSeek
vào nhịp chậm mà tổng vượt mốc đó, view buộc phải tự cắt và trả 504 có cấu trúc — đúng
lỗi người dùng thấy (xem transcribe_views.run_transcribe_upload_core). Nới hằng số chỉ
đẩy vấn đề sang trần kế tiếp.

Giải pháp: mô hình job nền + poll GIỐNG voice-clone (clone_voice_start_api). Client POST
để tạo job → nhận job_id ngay → poll GET .../transform-jobs/<id>/ mỗi ~3s. Mọi round-trip
đều < 1s nên không đụng bất kỳ trần timeout nào (app-level, gunicorn, hay edge proxy).

- PR1: helper spawn/poll/cancel + 2 endpoint status/cancel.
- PR2 (file này, phần dưới): 2 endpoint TẠO job — transcribe_upload_start +
  transform_content_upgrade_start — mỗi cái gói lại đúng phần lõi của endpoint đồng bộ
  tương ứng (run_transcribe_upload_core / PaastAnalysisService.upgrade_scripted) và chạy
  trong thread nền. Endpoint đồng bộ cũ GIỮ NGUYÊN cho tương thích ngược.

Tái dùng mix_progress_store (Redis-backed, fallback RAM, TTL 4h) — cùng store mà
voice-clone / mix / video-downloader đang dùng.
"""
import logging
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes, throttle_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from video_management.views.mix_progress_store import progress_get, progress_set, progress_update
from video_management.views.transcribe_views import (
    MAX_UPLOAD_SIZE_MB,
    TranscribeUploadThrottle,
    _get_ffmpeg,
    run_transcribe_upload_core,
)

logger = logging.getLogger(__name__)

# Prefix riêng trong progress store dùng chung.
_CT_JOB_PREFIX = "content_transform_job:"

# Trạng thái job — khớp 1-1 với những gì BE pollContentTransformJob() phân nhánh.
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_ERROR = "error"
JOB_CANCELLED = "cancelled"

_TERMINAL_STATUSES = frozenset({JOB_COMPLETED, JOB_ERROR, JOB_CANCELLED})


def _job_key(job_id: str) -> str:
    return f"{_CT_JOB_PREFIX}{job_id}"


def _write(job_id: str, **fields: Any) -> None:
    """Ghi/merge field vào progress store, LUÔN kèm updated_at làm heartbeat.

    Reconciliation của BE dựa vào updated_at để phân biệt job đang tiến triển với job
    "chết cứng" (thread bị giết do AI service restart) — nên mọi lần chạm store đều phải
    làm mới mốc này.
    """
    fields["updated_at"] = time.time()
    progress_update(_job_key(job_id), fields)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return progress_get(_job_key(job_id))


def is_cancelled(job_id: str) -> bool:
    data = get_job(job_id)
    return bool(data) and data.get("status") == JOB_CANCELLED


def spawn_job(
    kind: str,
    work_fn: Callable[[Callable[[], bool]], Dict[str, Any]],
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """Tạo job nền, trả job_id NGAY.

    `work_fn(check_cancel)` chạy trong thread daemon và nên trả về một dict chuẩn hoá:
      thành công        → {'success': True, ...payload...}
      lỗi có kiểm soát  → {'success': False, 'error_message': '...'}
    Ném exception cũng được — sẽ thành status 'error' với message là str(exc).

    `check_cancel()` trả True khi người dùng đã huỷ; work_fn nên kiểm giữa các bước dài.
    """
    job_id = uuid.uuid4().hex
    now = time.time()
    progress_set(_job_key(job_id), {
        "status": JOB_QUEUED,
        "kind": kind,
        "message": "Đang chờ xử lý...",
        "created_at": now,
        "updated_at": now,
        **(meta or {}),
    })

    def _runner() -> None:
        _write(job_id, status=JOB_RUNNING, message="Đang xử lý...")
        try:
            result = work_fn(lambda: is_cancelled(job_id))
        except Exception as e:  # noqa: BLE001 — mọi lỗi của work_fn phải thành status error, không nuốt im
            logger.error(f"[content-transform job {job_id}/{kind}] lỗi: {e}", exc_info=True)
            _write(job_id, status=JOB_ERROR, message=str(e), error=str(e))
            return

        if is_cancelled(job_id):
            # Người dùng huỷ trong lúc work_fn còn chạy — giữ nguyên trạng thái cancelled và
            # bỏ kết quả (kể cả khi work_fn vừa chạy xong): client đã thôi chờ.
            _write(job_id, message="Đã huỷ bởi người dùng.")
            return

        if isinstance(result, dict) and result.get("success") is False:
            msg = result.get("error_message") or "Xử lý thất bại."
            _write(job_id, status=JOB_ERROR, message=msg, error=msg, result=result)
            return

        _write(job_id, status=JOB_COMPLETED, message="Hoàn tất.", result=result)

    threading.Thread(target=_runner, name=f"ct_job_{kind}_{job_id}", daemon=True).start()
    return job_id


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def transform_job_status(request, job_id: str):
    """GET /api/content/transform-jobs/<job_id>/ — trạng thái 1 job.

    Response: { success, status, kind, message, updated_at, result?, error? }
    404 khi job_id không tồn tại (hết TTL 4h, sai id, hoặc AI service đã restart khiến
    job chạy bằng RAM fallback bị mất) — BE dịch cái này thành "job mất dấu".
    """
    data = get_job(job_id)
    if data is None:
        return Response({"success": False, "error": "job not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response({"success": True, **data}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def transform_job_cancel(request, job_id: str):
    """POST /api/content/transform-jobs/<job_id>/cancel/ — đánh dấu huỷ.

    Best-effort: set status='cancelled' trong store; thread nền tự thấy qua check_cancel()
    giữa các bước. Lệnh gọi LLM/Gemini đang chạy dở vẫn chạy nốt rồi kết quả bị bỏ.
    """
    data = get_job(job_id)
    if data is None:
        return Response({"success": False, "error": "job not found"}, status=status.HTTP_404_NOT_FOUND)
    if data.get("status") in _TERMINAL_STATUSES:
        # Đã xong/đã lỗi/đã huỷ rồi thì huỷ thêm không còn nghĩa gì — trả nguyên trạng thái.
        return Response({"success": True, "status": data.get("status"), "already_final": True})
    _write(job_id, status=JOB_CANCELLED, message="Đang huỷ...")
    return Response({"success": True, "status": JOB_CANCELLED})


# ═══════════════════════════════════════════════════════════════════════════════
# PR2 — endpoint TẠO job. Mỗi cái mở gói phần lõi của endpoint đồng bộ tương ứng và
# chạy trong thread nền; endpoint đồng bộ cũ giữ nguyên.
# ═══════════════════════════════════════════════════════════════════════════════

# Ngân sách thời gian rộng cho job nền: không còn bị 1 kết nối HTTP đồng bộ bó lại, chỉ cần
# đủ cho lần chạy Gemini/DeepSeek chậm nhất quan sát được cộng biên. Kẹp theo trần của
# _read_transcribe_budget (900s).
_JOB_TRANSCRIBE_BUDGET_S = 900
_JOB_UPGRADE_BUDGET_S = 900


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([TranscribeUploadThrottle])
@parser_classes([MultiPartParser, FormParser])
def transcribe_upload_start(request):
    """POST /api/content/transcribe-upload/start/ — bản job nền của transcribe_upload.

    Validate + ghi file ra đĩa NGAY trong request (như clone_voice_start_api), rồi spawn
    thread gọi run_transcribe_upload_core() với ngân sách rộng. Trả { success, job_id }.

    Giữ đúng IsAuthenticated + TranscribeUploadThrottle (10/phút) như bản đồng bộ — mỗi job
    vẫn tốn 1 lệnh gọi Gemini tính phí.
    """
    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return Response({"success": False, "error_message": "No file uploaded"}, status=400)

    file_size_mb = uploaded_file.size / (1024 * 1024)
    if file_size_mb > MAX_UPLOAD_SIZE_MB:
        return Response(
            {"success": False, "error_message": f"Dung lượng tập tin vượt quá giới hạn cho phép ({file_size_mb:.1f}MB > {MAX_UPLOAD_SIZE_MB}MB)."},
            status=400,
        )

    ffmpeg_path = _get_ffmpeg()
    if not ffmpeg_path:
        return Response({"success": False, "error_message": "FFmpeg not found on server"}, status=500)

    uid = uuid.uuid4().hex[:8]
    ext = os.path.splitext(uploaded_file.name)[1].lower() or ".mp4"
    input_path = os.path.join(tempfile.gettempdir(), f"vcb_upload_job_{uid}{ext}")
    with open(input_path, "wb+") as dest:
        for chunk in uploaded_file.chunks():
            dest.write(chunk)

    budget = _JOB_TRANSCRIBE_BUDGET_S

    def _work(check_cancel: Callable[[], bool]) -> Dict[str, Any]:
        started = time.time()
        try:
            return run_transcribe_upload_core(
                input_path,
                ffmpeg_path,
                deadline=started + budget,
                request_started_at=started,
                total_budget=budget,
                check_cancel=check_cancel,
            )
        finally:
            try:
                if os.path.exists(input_path):
                    os.remove(input_path)
            except OSError as e:
                logger.warning(f"[transcribe job] không xoá được file tạm {input_path}: {e}")

    job_id = spawn_job("transcribe", _work, meta={"message": "Đang nghe và chuyển đổi nội dung..."})
    return Response({"success": True, "job_id": job_id}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def transform_content_upgrade_start(request):
    """POST /api/ai/transform-content/upgrade/start/ — bản job nền của transform_content_upgrade.

    Body giống bản đồng bộ: write_system_prompt, write_user_prompt, max_tokens (mặc định
    16000). Spawn thread gọi PaastAnalysisService.upgrade_scripted(); trả { success, job_id }.

    Kết quả job (khi completed) có shape KHỚP response bản đồng bộ để BE map 1-1:
      { success, output_text, score, score_error, usage, model_used }
    Lỗi ở bước VIẾT (upgrade_scripted ném RuntimeError) → job status 'error'.
    """
    from video_management.services.paast_analysis_service import PaastAnalysisService

    write_system_prompt = (request.data.get("write_system_prompt") or "").strip()
    write_user_prompt = (request.data.get("write_user_prompt") or "").strip()
    if not write_system_prompt or not write_user_prompt:
        return Response(
            {"success": False, "error": "write_system_prompt và write_user_prompt là bắt buộc"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    max_tokens_raw = request.data.get("max_tokens")
    max_tokens = int(max_tokens_raw) if max_tokens_raw is not None else 16000
    # Giữ nguyên để BE poll/reconcile lấy lại "previous" mà không cần thêm cột DB.
    client_context = request.data.get("client_context")

    def _work(_check_cancel: Callable[[], bool]) -> Dict[str, Any]:
        result = PaastAnalysisService().upgrade_scripted(
            write_system_prompt=write_system_prompt,
            write_user_prompt=write_user_prompt,
            max_tokens=max_tokens,
            timeout_seconds=_JOB_UPGRADE_BUDGET_S,
        )
        return {
            "success": True,
            "output_text": result["output_text"],
            "score": result["new_analysis"],
            "score_error": result["score_error"],
            "usage": result["usage"],
            "model_used": result["model_used"],
        }

    meta = {"message": "Đang nâng cấp nội dung theo gợi ý..."}
    if client_context is not None:
        meta["client_context"] = client_context
    job_id = spawn_job("upgrade", _work, meta=meta)
    return Response({"success": True, "job_id": job_id}, status=status.HTTP_200_OK)
