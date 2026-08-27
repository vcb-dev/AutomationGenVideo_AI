"""Job nền + poll cho module content-transform (Chuyển đổi nội dung).

Vì sao cần: transcribe file dài và upgrade kịch bản dài chạy hàng trăm giây trong
MỘT request đồng bộ. Django neo ngân sách ~415s ngay khi vào view; khi Gemini/DeepSeek
vào nhịp chậm mà tổng vượt mốc đó, view buộc phải tự cắt và trả 504 có cấu trúc — đúng
lỗi người dùng thấy (xem transcribe_views.run_transcribe_upload_core). Nới hằng số chỉ
đẩy vấn đề sang trần kế tiếp.

Giải pháp: mô hình job nền + poll GIỐNG voice-clone (clone_voice_start_api). Client POST
để tạo job → nhận job_id ngay → poll GET .../transform-jobs/<id>/ mỗi ~3s. Mọi round-trip
đều < 1s nên không đụng bất kỳ trần timeout nào (app-level, gunicorn, hay edge proxy).

PR1 (file này) chỉ dựng KHUNG dùng lại được: helper spawn/poll/cancel + 2 endpoint
status/cancel. Các endpoint tạo job thật (transcribe/upgrade start) + phần đấu nối BE/FE
nằm ở PR2.

Tái dùng mix_progress_store (Redis-backed, fallback RAM, TTL 4h) — cùng store mà
voice-clone / mix / video-downloader đang dùng.
"""
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from video_management.views.mix_progress_store import progress_get, progress_set, progress_update

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
