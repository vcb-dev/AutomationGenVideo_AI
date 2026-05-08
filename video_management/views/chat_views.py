import os
import json
import logging
import requests
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

SYSTEM_PROMPT = """Bạn là VCB Assistant — trợ lý AI thông minh của hệ thống VCB Studio.
Bạn có thể trả lời câu hỏi, phân tích dữ liệu, và tạo dashboard trực quan.

Khi người dùng hỏi về dữ liệu/báo cáo, hãy trả về JSON có cấu trúc sau:
{
  "message": "Giải thích ngắn gọn kết quả",
  "dashboard": {
    "layout": "single" | "mixed",
    "blocks": [
      // Các block dashboard theo định dạng bên dưới
    ]
  }
}

Các loại block được hỗ trợ:

1. KPI Cards:
{
  "type": "kpi_card",
  "data": [
    { "label": "Tên chỉ số", "value": "Giá trị", "trend": "+12%", "trendUp": true }
  ]
}

2. Bar Chart:
{
  "type": "bar",
  "title": "Tiêu đề",
  "xKey": "name",
  "yKey": "value",
  "color": "#8b5cf6",
  "data": [{ "name": "Label", "value": 100 }]
}

3. Line Chart:
{
  "type": "line",
  "title": "Tiêu đề",
  "xKey": "date",
  "yKey": "value",
  "color": "#06b6d4",
  "data": [{ "date": "T1", "value": 100 }]
}

4. Pie Chart:
{
  "type": "pie",
  "title": "Tiêu đề",
  "data": [{ "name": "Label", "value": 100 }]
}

5. Table:
{
  "type": "table",
  "title": "Tiêu đề",
  "columns": ["Cột 1", "Cột 2"],
  "data": [{ "Cột 1": "A", "Cột 2": "B" }]
}

Quy tắc chọn chart:
- Xu hướng theo thời gian → line
- So sánh giữa các nhóm → bar
- Tỷ lệ phần trăm → pie
- Số liệu tổng hợp nổi bật → kpi_card
- Danh sách chi tiết → table
- Câu hỏi tổng hợp → mixed (nhiều block)

Nếu câu hỏi chỉ là hội thoại thông thường (không cần chart), trả về:
{ "message": "Câu trả lời của bạn" }

Luôn trả lời bằng tiếng Việt. Chỉ trả về JSON thuần, không có markdown.
"""


def _get_deepseek_key() -> str:
    key = str(getattr(settings, 'DEEPSEEK_API_KEY', '')).strip()
    if not key:
        key = os.getenv('DEEPSEEK_API_KEY', '').strip()
    return key


def _call_deepseek(messages: list, temperature: float = 0.7) -> dict | None:
    key = _get_deepseek_key()
    if not key:
        return None

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
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
    Body: { "message": "...", "history": [{"role": "user"|"assistant", "content": "..."}] }
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

    result = _call_deepseek(messages)

    if result is None:
        return Response(
            {"message": "Xin lỗi, tôi gặp sự cố kết nối. Vui lòng thử lại."},
            status=status.HTTP_200_OK,
        )

    return Response(result, status=status.HTTP_200_OK)
