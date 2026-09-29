"""Đơn giá TikHub theo endpoint (USD/lượt) — lấy từ CHÍNH TikHub để khớp hoá đơn.

Nguồn: GET /api/v1/tikhub/user/get_all_endpoints_info (endpoint_cost). Gọi endpoint này MIỄN PHÍ
— đã kiểm 2026-09-28: số dư tài khoản không đổi sau khi gọi. Đệm 24h; TikHub lỗi thì dùng bảng
dự phòng đo cùng ngày. Đây là giá NIÊM YẾT — TikHub có chiết khấu bậc thang theo lượng dùng nên
hoá đơn thật có thể thấp hơn một chút (con số thống kê là mức trần).
"""
import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_KEY = 'tikhub:endpoint_prices:v1'
CACHE_TTL = 24 * 3600
# Lấy bảng giá hỏng thì nghỉ một lúc mới thử lại — không để mọi lượt TikHub kéo theo một lượt tra giá.
FAILED_KEY = 'tikhub:endpoint_prices:failed'
FAILED_TTL = 600
DEFAULT_PRICE = 0.001
# Đo 2026-09-28 từ get_all_endpoints_info — dùng khi không lấy được bảng giá sống.
FALLBACK_PRICES = {
    '/api/v1/xiaohongshu/app_v2/get_video_note_detail': 0.01,
    '/api/v1/kuaishou/web/fetch_one_video': 0.002,
    '/api/v1/youtube/web_v2/get_video_streams_v2': 0.004,
}


def _load_prices() -> dict:
    prices = cache.get(CACHE_KEY)
    if isinstance(prices, dict) and prices:
        return prices
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    base = getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')
    if not api_key or cache.get(FAILED_KEY):
        return {}
    try:
        resp = requests.get(f'{base}/api/v1/tikhub/user/get_all_endpoints_info',
                            headers={'Authorization': f'Bearer {api_key}'}, timeout=15)
        items = (resp.json() or {}).get('data') if resp.status_code == 200 else None
        prices = {e['endpoint_uri']: float(e['endpoint_cost']) for e in items or []
                  if isinstance(e, dict) and e.get('endpoint_uri') and e.get('endpoint_cost') is not None}
    except Exception as e:  # noqa: BLE001 — không lấy được giá thì dùng bảng dự phòng
        logger.warning(f'[TIKHUB-PRICE] không lấy được bảng giá: {e}')
        prices = {}
    if prices:
        cache.set(CACHE_KEY, prices, CACHE_TTL)
    else:
        cache.set(FAILED_KEY, True, FAILED_TTL)
    return prices


def price_of(path: str) -> float:
    prices = _load_prices()
    if path in prices:
        return prices[path]
    return FALLBACK_PRICES.get(path, DEFAULT_PRICE)
