import os
import json
import logging
import requests
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
from video_management.services.social_fetch_service import TOOL_DEFINITIONS, call_tool

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

SYSTEM_PROMPT = """Bạn là VCB Assistant — trợ lý AI của hệ thống VCB Studio.
Bạn có quyền truy cập dữ liệu thực từ Facebook, Instagram, YouTube, TikTok và database nội bộ.

Khi người dùng hỏi về dữ liệu, LUÔN gọi tool phù hợp để lấy dữ liệu thực trước khi trả lời.

Sau khi có dữ liệu thực, trả về JSON có cấu trúc:
{
  "message": "Giải thích ngắn gọn kết quả",
  "dashboard": {
    "layout": "single" | "mixed",
    "blocks": [ ...các block... ]
  }
}

Các loại block:

1. KPI Cards — dùng cho số liệu tổng hợp nổi bật:
{ "type": "kpi_card", "data": [{ "label": "...", "value": "...", "trend": "+12%", "trendUp": true }] }

2. Bar Chart — so sánh giữa các nhóm:
{ "type": "bar", "title": "...", "xKey": "name", "yKey": "value", "color": "#8b5cf6", "data": [...] }

3. Line Chart — xu hướng theo thời gian:
{ "type": "line", "title": "...", "xKey": "date", "yKey": "value", "color": "#06b6d4", "data": [...] }

4. Pie Chart — tỷ lệ phần trăm:
{ "type": "pie", "title": "...", "data": [{ "name": "...", "value": 100 }] }

5. Table — danh sách chi tiết:
{ "type": "table", "title": "...", "columns": ["Cột 1", ...], "data": [{...}] }

Quy tắc:
- Xu hướng thời gian → line
- So sánh nhóm/team/platform → bar
- Tỷ lệ phần trăm → pie
- Số liệu tổng hợp → kpi_card
- Danh sách chi tiết → table
- Câu hỏi tổng hợp → mixed (nhiều block)

Nếu câu hỏi chỉ là hội thoại, trả về: { "message": "..." }

Luôn trả lời tiếng Việt. Chỉ trả về JSON thuần, không có markdown.
Format số lớn: 1.200.000 thành "1.2M", 45000 thành "45K".
"""


def _get_deepseek_key() -> str:
    key = str(getattr(settings, 'DEEPSEEK_API_KEY', '')).strip()
    return key or os.getenv('DEEPSEEK_API_KEY', '').strip()


def _call_deepseek_with_tools(messages: list) -> dict | None:
    """Gọi DeepSeek với function calling, tự động gọi tools nếu cần."""
    key = _get_deepseek_key()
    if not key:
        return None

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }

    # Bước 1: Gọi DeepSeek với tool definitions
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "tools": [{"type": "function", "function": t} for t in TOOL_DEFINITIONS],
        "tool_choice": "auto",
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    try:
        resp = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        choice = result["choices"][0]
        msg = choice["message"]

        # Nếu AI không gọi tool → trả về trực tiếp (câu hỏi hội thoại)
        if choice.get("finish_reason") != "tool_calls" or not msg.get("tool_calls"):
            # Thử parse JSON response
            content = msg.get("content", "")
            try:
                return json.loads(content)
            except Exception:
                return {"message": content}

        # Bước 2: Thực thi các tool calls
        tool_calls = msg["tool_calls"]
        messages_with_tools = messages + [msg]

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except Exception:
                fn_args = {}

            logger.info(f"Calling tool: {fn_name} args={fn_args}")
            tool_result = call_tool(fn_name, fn_args)

            messages_with_tools.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(tool_result, ensure_ascii=False, default=str),
            })

        # Bước 3: Gọi lại DeepSeek với kết quả tool, yêu cầu trả JSON dashboard
        final_payload = {
            "model": "deepseek-chat",
            "messages": messages_with_tools,
            "temperature": 0.3,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        }
        final_resp = requests.post(DEEPSEEK_API_URL, headers=headers, json=final_payload, timeout=60)
        final_resp.raise_for_status()
        content = final_resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)

    except json.JSONDecodeError as e:
        logger.error(f"DeepSeek JSON parse error: {e}")
        return None
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        return None


@api_view(["POST"])
def chat(request):
    """
    POST /api/chat/
    Body: { "message": "...", "history": [...] }
    """
    message = (request.data.get("message") or "").strip()
    history = request.data.get("history") or []

    if not message:
        return Response({"error": "message is required"}, status=status.HTTP_400_BAD_REQUEST)

    key = _get_deepseek_key()
    if not key:
        return Response({"error": "DEEPSEEK_API_KEY not configured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for item in history[-10:]:
        role = item.get("role", "user")
        content = item.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": message})

    result = _call_deepseek_with_tools(messages)

    if result is None:
        return Response(
            {"message": "Xin lỗi, tôi gặp sự cố kết nối. Vui lòng thử lại."},
            status=status.HTTP_200_OK,
        )

    return Response(result, status=status.HTTP_200_OK)
