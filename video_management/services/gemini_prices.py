"""Giá Gemini (USD / 1 triệu token) — ước tính chi phí theo số token Gemini trả về.

Nguồn: ai.google.dev/gemini-api/docs/pricing (đọc 2026-09-28), giá gói TRẢ PHÍ. Khoá đang ở gói
miễn phí thì Google không thu tiền — con số thống kê khi đó là "nếu bật billing sẽ tốn".
Token "suy nghĩ" (thinking) tính theo giá output. Đổi giá: đặt GEMINI_PRICES_JSON, vd
{"gemini-3.1-flash-lite": [[null, 0.25, 1.5, 0.5]]} — [áp dụng từ ngày, input, output, input âm thanh].

Đo thật (count_tokens, 2026-09-28): video 480p ≈ 75-110 token/giây với gemini-3.8-flash — một
clip 66s ≈ 7.100 token đầu vào ≈ 0,005 USD, rẻ hơn nhiều so với con số 720p trên trang giá.
"""
import json
import os
from datetime import date
from typing import Optional

# model → các mốc giá theo thời gian: (áp dụng từ ngày | None, input, output, input ÂM THANH | None).
# input âm thanh None = trang giá không tách riêng → tính như input thường.
DEFAULT_PRICES = {
    'gemini-3.8-flash': [(None, 0.75, 3.75, None), ('2027-01-01', 1.50, 7.50, None)],
    'gemini-3.5-flash': [(None, 1.50, 9.00, None)],
    'gemini-2.5-flash': [(None, 0.30, 2.50, 1.00)],
    'gemini-3.1-flash-lite': [(None, 0.25, 1.50, 0.50)],
    'gemini-3.5-transcribe': [(None, 2.00, 12.00, 2.00)],
}
# Tên luôn trỏ bản mới nhất — tính như bản mới nhất đã biết giá.
ALIASES = {
    'gemini-flash-latest': 'gemini-3.8-flash',
    'gemini-flash-lite-latest': 'gemini-3.1-flash-lite',
    'gemini-3.1-flash-lite-preview': 'gemini-3.1-flash-lite',
}
UNKNOWN_MODEL_PRICE = (0.75, 3.75, 0.75)
# Gemini tính âm thanh 25 token/giây (trang giá). Thư viện google.generativeai không trả số token
# theo loại (âm thanh/hình), nên ước phần âm thanh theo độ dài để tính đúng giá âm thanh.
AUDIO_TOKENS_PER_SECOND = 25


def _table() -> dict:
    raw = os.getenv('GEMINI_PRICES_JSON', '').strip()
    if not raw:
        return DEFAULT_PRICES
    try:
        custom = {k: [tuple(list(x) + [None] * (4 - len(x))) for x in v] for k, v in json.loads(raw).items()}
        return {**DEFAULT_PRICES, **custom}
    except (ValueError, TypeError):
        return DEFAULT_PRICES


def _pick(tiers, on: date) -> tuple:
    chosen = tiers[0]
    for tier in tiers:
        if tier[0] is None or date.fromisoformat(tier[0]) <= on:
            chosen = tier
    price_in, price_out = float(chosen[1]), float(chosen[2])
    return price_in, price_out, (float(chosen[3]) if chosen[3] is not None else price_in)


def prices_for(model: str, on: Optional[date] = None) -> tuple:
    """(input, output, input âm thanh) USD / 1 triệu token của model tại ngày `on` — luôn đủ 3 số."""
    on = on or date.today()
    name = ALIASES.get(model, model)
    for table in (_table(), DEFAULT_PRICES):
        if not table.get(name):
            continue
        try:
            return _pick(table[name], on)
        except (TypeError, ValueError, IndexError):
            continue  # dòng giá trong GEMINI_PRICES_JSON sai định dạng → dùng bảng mặc định
    return UNKNOWN_MODEL_PRICE


def cost_usd(model: str, input_tokens: int, output_tokens: int, on: Optional[date] = None,
             audio_tokens: int = 0) -> float:
    """`audio_tokens`: phần của input là âm thanh (tính giá âm thanh), không vượt quá input_tokens."""
    price_in, price_out, price_audio = prices_for(model, on)
    audio = max(0, min(int(audio_tokens or 0), int(input_tokens or 0)))
    return ((input_tokens - audio) * price_in + audio * price_audio + output_tokens * price_out) / 1_000_000
