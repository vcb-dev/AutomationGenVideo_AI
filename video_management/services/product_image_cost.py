"""
Chi phí từng lượt Gemini của "Tạo ảnh sản phẩm" — ước tính từ số token Gemini trả về × đơn giá
model, để BE lưu từng lượt và thống kê ở tab Chi phí.

Đơn giá đặt trong biến môi trường GEMINI_IMAGE_PRICES_JSON (USD / 1 triệu token), gắn theo TÊN
model — đổi GEMINI_MODEL mà quên cập nhật giá thì lộ ra ngay ("chưa có đơn giá cho model X")
thay vì âm thầm tính theo giá của model khác:
    {"gemini-3.1-flash-image": {"input": 0.5, "output_text": 3, "output_image": 60}}
Nguồn: ai.google.dev/gemini-api/docs/pricing (cập nhật 2026-09-24) — ảnh 1K = 1120 token ≈ $0,067.

Thư viện google.generativeai 0.8.x chỉ trả 4 con số (prompt / candidates / cached / total token),
không tách token ảnh, chữ và "suy nghĩ" — trong khi Google tính ba giá khác nhau cho ba loại đó:
- token suy nghĩ = total − prompt − candidates (API tính suy nghĩ vào total, không vào candidates)
  → giá output_text;
- token chữ trong phần trả lời ≈ số ký tự / 4 → giá output_text;
- phần còn lại của candidates là token ảnh → giá output_image.
Sai số chỉ nằm ở phần ước lượng chữ (vài chục token) — đối chiếu với trang billing khi test.
"""
import json
import math
import os

PRICES_ENV = 'GEMINI_IMAGE_PRICES_JSON'
PRICE_KEYS = ('input', 'output_text', 'output_image')

# Ước lượng token cho phần chữ Gemini trả kèm ảnh — tiếng Anh ~4 ký tự/token.
CHARS_PER_TEXT_TOKEN = 4


def read_model_price(model):
    """(bảng giá của model | None, lý do không có giá | None). Không bao giờ ném.

    Thiếu/sai giá KHÔNG chặn việc tạo ảnh — lượt đó vẫn được ghi nhưng chưa tính được tiền, và lý
    do (nêu tên biến) hiện ngay cho người bấm lẫn ở tab Chi phí.
    """
    raw = (os.environ.get(PRICES_ENV) or '').strip()
    if not raw:
        return None, f'Thiếu {PRICES_ENV} trên AI Service — chưa tính được tiền lượt này.'
    try:
        table = json.loads(raw)
    except ValueError:
        return None, f'{PRICES_ENV} không phải JSON hợp lệ — chưa tính được tiền lượt này.'
    entry = table.get(model) if isinstance(table, dict) else None
    if not isinstance(entry, dict):
        return None, f'{PRICES_ENV} chưa có đơn giá cho model {model} — chưa tính được tiền lượt này.'
    try:
        price = {key: float(entry[key]) for key in PRICE_KEYS}
    except (KeyError, TypeError, ValueError):
        price = None
    if price is None or any(value < 0 or math.isnan(value) for value in price.values()):
        return None, (
            f'Đơn giá model {model} trong {PRICES_ENV} phải có đủ {", ".join(PRICE_KEYS)} là số ≥ 0.'
        )
    return price, None


def split_tokens(usage_metadata, texts):
    """Tách tổng token Gemini báo về thành các phần tính giá khác nhau (xem đầu file)."""
    prompt = int(getattr(usage_metadata, 'prompt_token_count', 0) or 0)
    candidates = int(getattr(usage_metadata, 'candidates_token_count', 0) or 0)
    total = int(getattr(usage_metadata, 'total_token_count', 0) or 0)
    thinking = max(0, total - prompt - candidates)
    text_chars = sum(len(t) for t in texts or [])
    text = min(candidates, math.ceil(text_chars / CHARS_PER_TEXT_TOKEN))
    return {
        'input_tokens': prompt,
        'thinking_tokens': thinking,
        'text_tokens': text,
        'image_tokens': candidates - text,
    }


def cost_usd(tokens, price):
    return (
        tokens['input_tokens'] * price['input']
        + (tokens['thinking_tokens'] + tokens['text_tokens']) * price['output_text']
        + tokens['image_tokens'] * price['output_image']
    ) / 1_000_000


def usage_from_response(model, response, texts):
    """Khối `usage` trả cho BE sau một lượt Gemini ĐÃ chạy (có ảnh hay không đều tốn token)."""
    metadata = getattr(response, 'usage_metadata', None)
    if metadata is None:
        return {
            'model': model,
            'input_tokens': 0,
            'output_tokens': 0,
            'cost_usd': None,
            'cost_note': 'Gemini không trả số token — chưa tính được tiền lượt này.',
        }
    tokens = split_tokens(metadata, texts)
    price, note = read_model_price(model)
    return {
        'model': model,
        'input_tokens': tokens['input_tokens'],
        # Mọi token phía trả lời (ảnh + chữ + suy nghĩ) — BE chỉ lưu tổng, tiền đã tính ở đây.
        'output_tokens': tokens['thinking_tokens'] + tokens['text_tokens'] + tokens['image_tokens'],
        'cost_usd': round(cost_usd(tokens, price), 6) if price else None,
        'cost_note': note,
    }


def usage_not_billed(model, reason):
    """Gemini từ chối trước khi xử lý (model 404, hết quota, lỗi phía Google) — không tính phí."""
    return {'model': model, 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0, 'cost_note': reason}


def usage_unknown(model, reason):
    """Không biết Gemini có tính phí không (vd hết thời gian chờ khi Google có thể vẫn đang xử lý)."""
    return {'model': model, 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': None, 'cost_note': reason}
