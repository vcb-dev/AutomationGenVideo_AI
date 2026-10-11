"""
Lớp AI cho luồng khớp video kênh nội bộ ↔ task (BE: task-video-match.service.ts).

BE chạy heuristic trước; chỉ những video heuristic BỎ TRỐNG (hoà điểm / tiêu đề không khớp chữ)
mới được gửi sang đây kèm danh sách task ứng viên ĐÃ qua mỏ neo cứng (cùng chủ kênh/team, cùng
tuyến, đăng trong cửa sổ ngày). DeepSeek chỉ trả lời: video này làm từ task nào — hoặc không task
nào. BE tự kiểm lại (task phải thuộc danh sách, đủ tự tin, task chưa có link cùng nền tảng).

Nhãn T1..Tn thay cho UUID trong prompt: model chép UUID hay sai 1-2 ký tự, nhãn ngắn thì không.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from django.conf import settings

from video_management.services.content_generation_service import (
    ContentGenerationService,
    DeepSeekError,
)

logger = logging.getLogger(__name__)

MAX_CASES_PER_REQUEST = 20
MAX_CANDIDATES_PER_CASE = 8
MAX_WORKERS = 4
# Phán quyết ngắn → tắt suy luận: nhanh, tất định, không bị reasoning_tokens ăn hết max_tokens.
MAX_TOKENS = 500
MAX_TOKENS_THINKING = 4000
TIMEOUT_S = 60
TIMEOUT_THINKING_S = 120
MAX_ATTEMPTS = 2
ALLOWED_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}

VN_TZ = timezone(timedelta(hours=7))

SYSTEM_MSG = (
    "Bạn là trợ lý đối soát video đã đăng với task sản xuất nội dung. Bạn cực kỳ thận trọng: "
    "bỏ trống tốt hơn chọn sai. Chỉ trả JSON hợp lệ, không markdown, không giải thích thêm."
)

RULES = """Bối cảnh: thương hiệu trang sức. Editor nhận task (tiêu đề content, kịch bản, sản phẩm), quay/dựng video rồi đăng lên Facebook/Instagram. Caption thường mở đầu bằng câu hook viết lại từ tiêu đề content (có thể diễn đạt khác, thêm/bớt chữ), sau đó là hashtag: #A1..#A5 = tuyến nội dung, #K... = mã cụm kênh, mã kiểu #N0011/#D0018 = SKU sản phẩm.
Mọi task bên dưới ĐÃ cùng người/kênh, cùng tuyến và gần ngày đăng — những điểm chung đó KHÔNG phải căn cứ để chọn.

Quy tắc:
1. Chỉ chọn một task khi Ý CHÍNH của video (món đồ/mẫu sản phẩm, câu chuyện, câu hỏi, cách làm) trùng với tiêu đề, kịch bản hoặc sản phẩm của task đó. Diễn đạt khác nhau nhưng cùng ý vẫn tính là trùng; một bên nói chung hơn bên kia (vd "trang sức" ↔ "vòng cổ") mà cùng ý chính vẫn là trùng.
2. MÂU THUẪN ⇒ KHÔNG khớp: khi video và task mỗi bên nêu một chi tiết cụ thể cùng loại (cách làm, vật dụng, mẫu sản phẩm, con số) mà hai chi tiết đó KHÁC nhau — vd "làm sáng bạc bằng kem đánh răng" ≠ "làm sáng bạc bằng baking soda"; "nhẫn cưới cho nam" ≠ "nhẫn cỏ 4 lá cho nữ"; "lịch sử đá ruby" ≠ "cách chọn đá hợp mệnh".
3. KHÔNG chọn chỉ vì trùng khuôn câu chung ("3 sự thật về…", "cách…", "mẹo…", "tại sao…"), chỉ trùng LOẠI sản phẩm/chất liệu (vàng, bạc, nhẫn, bông tai, dây chuyền, moissanite, trang sức) hoặc chỉ trùng hashtag thương hiệu (#vienchibao, #huyk…). Task có tiêu đề chỉ là tên sản phẩm chung chung thì chỉ chọn khi caption nêu đúng mẫu đó hoặc SKU trùng.
4. SKU trong caption trùng đúng SKU của một task ⇒ ưu tiên task đó (trừ khi mâu thuẫn rõ theo quy tắc 2). SKU có thể theo 2 hệ mã khác nhau nên SKU khác KHÔNG tự nó loại được task.
5. Nhiều task khớp ngang nhau, không phân biệt được ⇒ task = null. Không task nào khớp ý chính ⇒ task = null.
6. "Đã gắn video cùng nền tảng" = task đó đã có video khác. Nếu video đó cùng nội dung với video đang xét thì đây là bản đăng lại ⇒ vẫn chọn task đó; nếu khác nội dung thì task đó ít khả năng là đáp án.
7. confidence: 0.9–1.0 khi ý chính trùng rõ và chỉ một task khớp; 0.6–0.8 khi khá giống nhưng còn nghi ngờ; dưới 0.5 khi đoán."""


def _fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return "không rõ"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(VN_TZ).strftime("%d/%m/%Y %H:%M")


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def build_prompt(case: Dict[str, Any]) -> str:
    video = case.get("video") or {}
    candidates = (case.get("candidates") or [])[:MAX_CANDIDATES_PER_CASE]

    lines: List[str] = [RULES, "", "VIDEO:"]
    lines.append(f"- Nền tảng: {video.get('platform') or 'không rõ'}")
    lines.append(f"- Đăng lúc: {_fmt_time(video.get('published_at'))} (giờ VN)")
    lines.append(f"- Caption: {_clip(video.get('caption'), 1200)}")
    transcript = _clip(video.get("transcript"), 1500)
    if transcript:
        lines.append(f"- Lời thoại (phụ đề): {transcript}")

    lines.append("")
    lines.append("DANH SÁCH TASK:")
    for idx, c in enumerate(candidates, start=1):
        lines.append(f"[T{idx}]")
        lines.append(f"- Tiêu đề content: {_clip(c.get('title'), 200) or '(trống)'}")
        names = [n for n in (c.get("product_names") or []) if n]
        skus = [s for s in (c.get("product_skus") or []) if s]
        if names or skus:
            lines.append(f"- Sản phẩm: {', '.join(names) or '(không tên)'}" + (f" (SKU {', '.join(skus)})" if skus else ""))
        script = _clip(c.get("script"), 600)
        if script:
            lines.append(f"- Kịch bản (trích): {script}")
        days = c.get("days_from_video")
        when = _fmt_time(c.get("anchor_at"))
        lines.append(
            f"- Nộp/duyệt: {when}" + (f" (lệch {abs(float(days)):.1f} ngày so với lúc đăng)" if days is not None else "")
        )
        linked = [h for h in (c.get("linked_videos_same_platform") or []) if h is not None]
        if c.get("has_platform_link"):
            detail = "; ".join(f'"{_clip(h, 150)}"' for h in linked) if linked else "(không rõ nội dung)"
            lines.append(f"- Đã gắn video cùng nền tảng: {detail}")

    lines.append("")
    lines.append(
        "Trả về DUY NHẤT JSON (điền video_topic TRƯỚC rồi mới so): "
        '{"video_topic": "ý chính của video, ≤15 chữ", '
        '"task": "T1".."T%d" hoặc null, '
        '"task_topic": "ý chính của task được chọn, ≤15 chữ (null nếu không chọn)", '
        '"conflict": "chi tiết mâu thuẫn giữa video và task được chọn theo quy tắc 2, hoặc null nếu không có", '
        '"confidence": số 0..1, "reason": "1 câu tiếng Việt"}' % max(1, len(candidates))
    )
    return "\n".join(lines)


def parse_verdict(raw: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Nhãn T<n> → task_id. Nhãn lạ/ngoài danh sách ⇒ null (không bao giờ bịa task)."""
    label = raw.get("task")
    task_id = None
    if isinstance(label, str):
        s = label.strip().upper().lstrip("[").rstrip("]")
        if s.startswith("T") and s[1:].isdigit():
            idx = int(s[1:]) - 1
            if 0 <= idx < len(candidates):
                task_id = candidates[idx].get("task_id")
    try:
        confidence = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    # Model tự khai có chi tiết mâu thuẫn mà vẫn chọn ⇒ không tin lựa chọn đó.
    conflict = raw.get("conflict")
    if task_id and isinstance(conflict, str) and conflict.strip() and conflict.strip().lower() not in ("null", "không", "none"):
        task_id = None
    return {
        "task_id": task_id,
        "confidence": confidence if task_id else 0.0,
        "reason": _clip(raw.get("reason"), 300),
        "video_topic": _clip(raw.get("video_topic"), 120),
        "task_topic": _clip(raw.get("task_topic"), 120),
    }


def _judge_case(gen: ContentGenerationService, case: Dict[str, Any], model: str, thinking: bool = False) -> Dict[str, Any]:
    case_id = case.get("case_id")
    candidates = (case.get("candidates") or [])[:MAX_CANDIDATES_PER_CASE]
    if not candidates:
        return {"case_id": case_id, "task_id": None, "confidence": 0.0, "reason": "không có ứng viên"}

    prompt = build_prompt(case)
    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = gen._call_deepseek_checked(
                prompt=prompt,
                system_msg=SYSTEM_MSG,
                model=model,
                temperature=0,
                max_tokens=MAX_TOKENS_THINKING if thinking else MAX_TOKENS,
                timeout=TIMEOUT_THINKING_S if thinking else TIMEOUT_S,
                log_prefix=f"VIDEO-MATCH judge {case_id} lượt {attempt}",
                extra_params={
                    "thinking": {"type": "enabled" if thinking else "disabled"},
                    "response_format": {"type": "json_object"},
                },
            )
        except DeepSeekError as e:
            last_err = f"{e.kind}: {e}"
            if not e.retriable:
                break
            continue
        parsed = gen._extract_json_dict(raw)
        if parsed:
            return {"case_id": case_id, **parse_verdict(parsed, candidates)}
        last_err = "parse: không đọc được JSON"
    logger.warning(f"VIDEO-MATCH judge {case_id} hỏng: {last_err}")
    return {"case_id": case_id, "error": last_err or "unknown"}


def judge_cases(cases: List[Dict[str, Any]], model: Optional[str] = None, thinking: bool = False) -> Dict[str, Any]:
    if not isinstance(cases, list) or not cases:
        raise ValueError("Thiếu cases")
    if len(cases) > MAX_CASES_PER_REQUEST:
        raise ValueError(f"Tối đa {MAX_CASES_PER_REQUEST} cases mỗi request")
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"cases[{index}] phải là object")
        if not isinstance(case.get("video") or {}, dict):
            raise ValueError(f"cases[{index}].video phải là object")
        candidates = case.get("candidates") or []
        if not isinstance(candidates, list) or any(not isinstance(c, dict) for c in candidates):
            raise ValueError(f"cases[{index}].candidates phải là mảng object")

    configured_model = getattr(settings, "DEEPSEEK_MODEL", "deepseek-v4-flash")
    chosen = model if model in ALLOWED_MODELS else configured_model
    if chosen not in ALLOWED_MODELS:
        chosen = "deepseek-v4-flash"
    gen = ContentGenerationService()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(lambda c: _judge_case(gen, c, chosen, thinking), cases))
    return {"model": chosen, "thinking": thinking, "results": results}
