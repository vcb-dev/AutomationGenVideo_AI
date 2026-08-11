"""
Scraped Video Script Service.

Dịch caption/mô tả của 1 video đã cào (từ scraper subsystem) sang tiếng Việt +
viết phân tích ngắn cấu trúc video gốc (hook/thân/CTA). KHÔNG dùng chung với
task_script_service.py — service đó chuyên "adapt content đã win cho sản phẩm
mới" (cần productName/contentLine/...), không hợp để phân tích 1 video cào về
không gắn với sản phẩm/tuyến nội dung nào. Chỉ dùng lại helper gọi DeepSeek.
"""
import logging
from typing import Any, Dict, List, Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# URL/model đọc từ settings LÚC GỌI (không phải lúc import) — theo mẫu _tikhub_base():
# đổi .env là đổi được, và override_settings trong test cũng có tác dụng.
def _deepseek_base() -> str:
    return getattr(settings, 'DEEPSEEK_API_BASE_URL', 'https://api.deepseek.com')


def _deepseek_model() -> str:
    # DeepSeek đã bỏ tên "deepseek-chat" — chỉ còn deepseek-v4-pro / deepseek-v4-flash.
    return getattr(settings, 'DEEPSEEK_MODEL', 'deepseek-v4-flash')


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    import json
    import re

    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass

    md_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if md_match:
        try:
            return json.loads(md_match.group(1))
        except Exception:  # noqa: BLE001
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:  # noqa: BLE001
            pass

    return None


def _call_deepseek(prompt: str) -> Dict[str, Any]:
    deepseek_key = str(getattr(settings, "DEEPSEEK_API_KEY", "")).strip()
    if not deepseek_key:
        raise ValueError("DEEPSEEK_API_KEY chưa được cấu hình")

    res = requests.post(
        f"{_deepseek_base()}/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {deepseek_key}",
        },
        json={
            "model": _deepseek_model(),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )

    if not res.ok:
        try:
            err_body = res.json()
        except Exception:  # noqa: BLE001
            err_body = {}
        message = (err_body.get("error") or {}).get("message") or f"HTTP {res.status_code}"
        raise ValueError(f"DeepSeek API lỗi: {message}")

    data = res.json()
    raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")

    result = _extract_json(raw)
    if not result:
        raise ValueError("Không parse được kết quả từ AI")
    return result


def _build_prompt(p: Dict[str, Any]) -> str:
    hashtags: List[str] = p.get("hashtags") or []
    hashtags_text = ", ".join(hashtags) if hashtags else "(không có)"

    lines = [
        "Bạn là chuyên gia phân tích nội dung mạng xã hội (TikTok/Reels/Shorts).",
        "Bạn được cung cấp 1 video đã cào từ mạng xã hội (không phải nội dung nội bộ, "
        "không gắn với sản phẩm hay tuyến nội dung nào của công ty). Nhiệm vụ của bạn CHỈ có 2 việc:",
        "1. Dịch tiêu đề + mô tả video sang tiếng Việt tự nhiên (không dịch máy móc từng chữ).",
        "2. Viết phân tích NGẮN GỌN cấu trúc video gốc (dựa trên nội dung đã dịch + số liệu tương tác "
        "nếu có): hook mở đầu là gì, nội dung/thân bài triển khai thế nào, CTA (nếu có) là gì, vì sao "
        "video này có khả năng thu hút người xem.",
        "KHÔNG được sáng tác lại nội dung, KHÔNG adapt cho sản phẩm nào — chỉ mô tả/phân tích video gốc.",
        "",
        "═══ THÔNG TIN VIDEO ═══",
        f"Nền tảng: {p.get('platform') or '(không rõ)'}",
        f"Tiêu đề gốc: {p.get('title') or '(không có)'}",
        f"Mô tả gốc: {p.get('description') or '(không có)'}",
        f"Hashtags gốc: {hashtags_text}",
    ]

    engagement_parts = []
    if p.get("views_count") is not None:
        engagement_parts.append(f"{p['views_count']} lượt xem")
    if p.get("likes_count") is not None:
        engagement_parts.append(f"{p['likes_count']} lượt thích")
    if p.get("comments_count") is not None:
        engagement_parts.append(f"{p['comments_count']} bình luận")
    if engagement_parts:
        lines.append(f"Tương tác: {', '.join(engagement_parts)}")

    lines += [
        "",
        "CHỈ trả về JSON hợp lệ (không markdown, không giải thích), đúng format sau:",
        "{\n"
        '  "vietnamese_content": "tiêu đề + mô tả đã dịch sang tiếng Việt tự nhiên, viết liền mạch",\n'
        '  "script_outline": "phân tích ngắn gọn cấu trúc video gốc (hook/thân/CTA), khoảng 80-150 từ",\n'
        '  "hashtags": ["#tag1", "#tag2", "..."]\n'
        "}",
    ]
    return "\n".join(lines)


def analyze_scraped_video(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    params: { platform, title, description, hashtags: [str],
              views_count?, likes_count?, comments_count? }
    Returns: { vietnamese_content: str, script_outline: str, hashtags: [str] }
    """
    prompt = _build_prompt(params)
    return _call_deepseek(prompt)
