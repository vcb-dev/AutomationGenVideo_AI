"""Tình trạng tài khoản TikHub (số dư, chi tiêu hôm nay) — cho trang thống kê chi phí.

Hai endpoint tra tài khoản của TikHub đều MIỄN PHÍ (đã kiểm 2026-09-28: số dư không đổi sau khi
gọi). Đây là số liệu của CẢ TÀI KHOẢN (mọi tính năng dùng chung khoá), không riêng Bộ Sưu Tập —
dùng để đối chiếu với thống kê chi tiết mà BE tự ghi. TikHub chỉ trả số liệu NGÀY HIỆN TẠI theo
giờ America/Los_Angeles.
"""
import logging

import requests
from django.conf import settings
from django.core.cache import cache
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.tikhub_prices import price_of

logger = logging.getLogger(__name__)

CACHE_KEY = 'tikhub:account_summary:v1'
CACHE_TTL = 300


def _get(path: str):
    base = getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')
    resp = requests.get(f'{base}{path}', headers={'Authorization': f'Bearer {settings.TIKHUB_API_KEY}'}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def build_summary() -> dict:
    info = _get('/api/v1/tikhub/user/get_user_info')
    daily = (_get('/api/v1/tikhub/user/get_user_daily_usage') or {})
    user = info.get('user_data') or {}
    day = daily.get('data') or {}
    counts = day.get('uri_counts') or {}
    top = sorted(
        ({'endpoint': uri, 'count': n, 'cost_usd': round(n * price_of(uri), 4)} for uri, n in counts.items()),
        key=lambda x: -x['cost_usd'],
    )[:10]
    return {
        'key_name': (info.get('api_key_data') or {}).get('api_key_name'),
        'balance_usd': user.get('balance'),
        'free_credit_usd': user.get('free_credit'),
        'today': {
            'date': day.get('date'),
            'time_zone': daily.get('time_zone'),
            'usage_usd': round(float(day.get('usage') or 0), 4),
            'total_requests': day.get('total_request_per_day'),
            'paid_requests': day.get('paid_request_per_day'),
            'top_endpoints': top,
        },
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tikhub_account(request):
    """GET /api/tikhub/account/ — số dư + chi tiêu hôm nay của tài khoản TikHub (đệm 5 phút)."""
    if not getattr(settings, 'TIKHUB_API_KEY', ''):
        return Response({'error': 'Chưa cấu hình TIKHUB_API_KEY.'}, status=503)
    summary = cache.get(CACHE_KEY)
    if summary is None:
        try:
            summary = build_summary()
        except Exception as e:  # noqa: BLE001
            logger.warning(f'[TIKHUB-ACCOUNT] lỗi: {e}')
            return Response({'error': f'Không lấy được thông tin tài khoản TikHub: {e}'}, status=502)
        cache.set(CACHE_KEY, summary, CACHE_TTL)
    return Response(summary)
