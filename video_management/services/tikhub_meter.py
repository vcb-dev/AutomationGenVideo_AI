"""Đo MỌI lượt gọi TikHub tại một chỗ: móc vào tầng gửi của `requests` (HTTPAdapter.send).

Mười một tệp tikhub_*.py gọi TikHub thẳng bằng requests. Sửa từng tệp thì dễ sót, và tệp mới
nào cũng phải nhớ tự ghi. Móc ở tầng gửi thì lượt gọi nào tới host TikHub cũng được ghi vào
usage_meter khi đang có request đo chi phí (ApiUsageMiddleware bọc mọi request).

Tính tiền giữ đúng quy ước cũ của tikhub_cache: HTTP 200 → đơn giá của endpoint theo bảng giá
TikHub (kể cả khi thân báo lỗi — thà thống kê dư còn hơn thiếu); lỗi HTTP → 0đ vì TikHub không
tính lượt lỗi; lỗi mạng không có hồi đáp → không ghi. Endpoint tra tài khoản /api/v1/tikhub/user/*
miễn phí và do chính mình gọi để theo dõi → bỏ qua.
"""
import logging
from urllib.parse import urlsplit

from django.conf import settings
from requests.adapters import HTTPAdapter

from . import usage_meter
from .tikhub_prices import price_of

logger = logging.getLogger(__name__)

FREE_PREFIXES = ('/api/v1/tikhub/user/',)
_INSTALLED_MARK = '_vcb_tikhub_meter'


def _tikhub_hosts() -> set:
    hosts = {'api.tikhub.io', 'api.tikhub.dev'}
    base = getattr(settings, 'TIKHUB_API_BASE_URL', '') or ''
    if base:
        hosts.add((urlsplit(base).hostname or '').lower())
    return hosts


def observe(request, response) -> None:
    """Ghi một lượt gọi đã có hồi đáp vào usage_meter nếu đó là lượt gọi TikHub tính phí."""
    if not usage_meter.is_collecting():
        return
    parts = urlsplit(getattr(request, 'url', '') or '')
    if (parts.hostname or '').lower() not in _tikhub_hosts() or parts.path.startswith(FREE_PREFIXES):
        return
    status = getattr(response, 'status_code', None)
    usage_meter.record('tikhub', parts.path, cost_usd=price_of(parts.path) if status == 200 else 0, status=status)


def wrap(send):
    """Bọc một hàm send của HTTPAdapter: gửi như cũ rồi ghi lượt gọi."""
    def metered_send(self, request, *args, **kwargs):
        response = send(self, request, *args, **kwargs)
        try:
            observe(request, response)
        except Exception as e:  # noqa: BLE001 — đo chi phí không bao giờ được làm hỏng lượt gọi thật
            logger.warning('[TIKHUB-METER] Không ghi được lượt gọi TikHub: %s', e)
        return response

    setattr(metered_send, _INSTALLED_MARK, True)
    return metered_send


def install() -> None:
    """Gắn móc một lần cho cả tiến trình (gọi từ AppConfig.ready)."""
    if getattr(HTTPAdapter.send, _INSTALLED_MARK, False):
        return
    HTTPAdapter.send = wrap(HTTPAdapter.send)
