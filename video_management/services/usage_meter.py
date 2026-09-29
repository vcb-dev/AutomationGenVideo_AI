"""Đếm chi phí API bên thứ ba (TikHub, Gemini) trong MỘT request — để BE lưu và thống kê.

ApiUsageMiddleware bọc MỌI request trong `collect()` rồi gửi các sự kiện về BE qua header
X-Api-Usage. View nào cần trả chi phí trong thân phản hồi thì bọc thêm `collect()` của riêng nó:
các khối collect lồng nhau đều nhận đủ sự kiện. Chỗ gọi TikHub/Gemini chỉ cần gọi
usage_meter.record(...) (lượt gọi TikHub thật do tikhub_meter tự ghi). Ngoài mọi khối collect
thì record không làm gì.

Mỗi sự kiện: {provider, endpoint, cached, status, cost_usd, input_tokens, output_tokens}.
"""
import contextvars
from contextlib import contextmanager

# Chồng các danh sách đang gom — collect lồng nhau (middleware + view) đều phải nhận sự kiện.
_stack = contextvars.ContextVar('api_usage_stack', default=())


@contextmanager
def collect():
    events: list = []
    token = _stack.set((*_stack.get(), events))
    try:
        yield events
    finally:
        _stack.reset(token)


def is_collecting() -> bool:
    """Có request nào đang đo chi phí không — để chỗ gọi khỏi tốn công tính giá khi không cần."""
    return bool(_stack.get())


def record(provider: str, endpoint: str, cost_usd: float = 0.0, cached: bool = False,
           status=None, input_tokens: int = 0, output_tokens: int = 0) -> None:
    stack = _stack.get()
    if not stack:
        return
    event = {
        'provider': provider,
        'endpoint': endpoint,
        'cached': bool(cached),
        'status': status,
        'cost_usd': round(float(cost_usd or 0), 6),
        'input_tokens': int(input_tokens or 0),
        'output_tokens': int(output_tokens or 0),
    }
    for events in stack:
        events.append(dict(event))


def summarize(events: list) -> list:
    """Gộp các lượt giống nhau (cùng nhà cung cấp, endpoint, bộ đệm, mã HTTP) thành một dòng có
    `calls` — một lần đồng bộ kênh có thể gọi cùng endpoint nhiều trang, header phải gọn."""
    merged: dict = {}
    for e in events:
        key = (e['provider'], e['endpoint'], e['cached'], e['status'])
        row = merged.get(key)
        if row is None:
            merged[key] = {**e, 'calls': 1}
        else:
            row['calls'] += 1
            row['cost_usd'] = round(row['cost_usd'] + e['cost_usd'], 6)
            row['input_tokens'] += e['input_tokens']
            row['output_tokens'] += e['output_tokens']
    return list(merged.values())
