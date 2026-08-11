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

# Đọc settings LÚC GỌI (không phải lúc import) — đổi .env là đổi được, xem tests.test_ai_provider_urls
def _deepseek_chat_url() -> str:
    return f"{getattr(settings, 'DEEPSEEK_API_BASE_URL', 'https://api.deepseek.com')}/chat/completions"

SYSTEM_PROMPT = """Bạn là VCB Assistant — trợ lý AI của hệ thống VCB Studio.
Bạn có quyền truy cập dữ liệu thực từ Facebook, Instagram, YouTube, TikTok và database nội bộ.

Khi người dùng hỏi về dữ liệu, LUÔN gọi tool phù hợp để lấy dữ liệu thực trước khi trả lời.

═══════════════════════════════════════════════════
📋 RULE NỘI BỘ PHÂN TÍCH QUẢNG CÁO VCB
═══════════════════════════════════════════════════

## Hệ thống Content
- A1 → A3: Content nuôi (tăng view, trust)
- A4: Content chốt (tạo mess, doanh thu) — CHỈ A4 mới được chạy mess
- A5: Content hybrid (vừa nuôi vừa bán)

## Phân loại Camp
- like_page: Follow page
- tuong_tac: Tương tác (comment, share, reaction)
- mess: Tin nhắn — QUAN TRỌNG NHẤT

## Rule đánh giá

### LIKE PAGE
- ≤ 1.000đ/follow → TỐT → SCALE
- 1.000–1.500đ → TRUNG BÌNH → TEST
- > 1.500đ → KÉM → STOP

### TƯƠNG TÁC
- ~12đ/tương tác → TỐT
- > 20đ → TRUNG BÌNH
- Quá cao → STOP (không cần scale mạnh)

### MESS (QUAN TRỌNG NHẤT)
- ≤ 10.000đ/mess → TỐT → SCALE
- > 10.000đ → KÉM → STOP

## Rule TẮT ADS
Nếu chạy > 3 ngày VÀ:
- Mess < 20 HOẶC cost > 15.000đ
- HOẶC: A1 > 20đ | A2,A3 > 30đ | A4,A5 > 40đ
→ STOP NGAY

## Rule SCALE / NHÂN NHÓM
- Mess > 30 VÀ cost < 8.000đ → SCALE MẠNH
- Like page ≤ 1.000đ → SCALE

## Thứ tự ưu tiên đánh giá: Mess > Like page > Tương tác

═══════════════════════════════════════════════════
Khi người dùng cung cấp dữ liệu camp ads (content_type, camp_type, cost_per_result, results, spend, days_running...),
hãy phân tích theo đúng rule trên và trả về JSON với dashboard gồm:
- Đánh giá: GOOD / WARNING / BAD
- Lý do (so với rule cụ thể)
- Hành động: SCALE / KEEP / TEST / STOP
- Insight: content win không, có nên chuyển camp không, có đang đốt tiền không
═══════════════════════════════════════════════════

═══════════════════════════════════════════════════
📊 FORMAT BÁO CÁO TRAFFIC TIKTOK
═══════════════════════════════════════════════════
Khi người dùng hỏi "báo cáo traffic tháng X" hoặc liên quan đến TikTok:
1. Gọi tool get_tiktok_monthly_report(year, month, team?, owner?)
2. Trả về dashboard dạng "mixed" với các blocks:
   - kpi_card: Tổng views, likes, comments, shares, số video, số kênh
   - table "Top 10 video views cao nhất": title, channel, owner, team, views
   - table "Top 10 video likes cao nhất": title, channel, owner, team, likes
   - table "Top 10 video comments cao nhất": title, channel, owner, team, comments
   - bar "Traffic theo team": team, total_views
   - table "Chi tiết từng kênh": channel, owner, team, videos, views, likes, comments
3. Format số: 1.200.000 → "1.2M", 45000 → "45K"
4. Nếu user hỏi lọc theo team/người → truyền tham số team/owner vào tool
═══════════════════════════════════════════════════

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


def _extract_json(text: str) -> dict:
    """Parse JSON từ response — xử lý cả trường hợp DeepSeek bọc trong markdown code block."""
    text = text.strip()
    # Strip markdown code block: ```json ... ``` hoặc ``` ... ```
    if text.startswith("```"):
        lines = text.split("\n")
        # Bỏ dòng đầu (```json) và dòng cuối (```)
        inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        text = inner.strip()
    return json.loads(text)


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
        resp = requests.post(_deepseek_chat_url(), headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        choice = result["choices"][0]
        msg = choice["message"]

        # Nếu AI không gọi tool → trả về trực tiếp
        if choice.get("finish_reason") != "tool_calls" or not msg.get("tool_calls"):
            content = msg.get("content", "")
            try:
                return _extract_json(content)
            except Exception:
                return {"message": content}

        # Bước 2: Thực thi tool calls
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

            # Giới hạn data trả về để tránh context quá lớn
            tool_json = json.dumps(tool_result, ensure_ascii=False, default=str)
            if len(tool_json) > 8000:
                # Cắt bớt nếu quá dài (giữ các items đầu)
                if isinstance(tool_result, list):
                    tool_result = tool_result[:20]
                elif isinstance(tool_result, dict) and "campaigns" in tool_result:
                    tool_result["campaigns"] = tool_result["campaigns"][:20]
                tool_json = json.dumps(tool_result, ensure_ascii=False, default=str)

            messages_with_tools.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_json,
            })

        # Bước 3: Gọi lại DeepSeek với kết quả tool
        final_payload = {
            "model": "deepseek-chat",
            "messages": messages_with_tools,
            "temperature": 0.3,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        }
        final_resp = requests.post(_deepseek_chat_url(), headers=headers, json=final_payload, timeout=60)
        final_resp.raise_for_status()
        content = final_resp.json()["choices"][0]["message"]["content"]
        try:
            return _extract_json(content)
        except json.JSONDecodeError as e:
            logger.error(f"DeepSeek JSON parse error: {e} | content[:200]: {content[:200]}")
            # Fallback: trả về message text thay vì crash
            return {"message": content[:500] if content else "Không thể xử lý phản hồi từ AI."}

    except json.JSONDecodeError as e:
        logger.error(f"DeepSeek outer JSON parse error: {e}")
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

    from datetime import datetime
    now = datetime.now()
    date_context = f"\n\nLƯU Ý: Hôm nay là ngày {now.day}, tháng {now.month}, năm {now.year}. Khi người dùng hỏi 'tháng này', hãy dùng month={now.month}, year={now.year}."
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT + date_context}]

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
