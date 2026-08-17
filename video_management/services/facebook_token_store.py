"""Nơi giữ User Access Token của Facebook và tự gia hạn trước khi hết hạn.

Vì sao cần file state thay vì đọc thẳng .env: Django nạp settings MỘT LẦN lúc boot,
nên gia hạn xong mà chỉ ghi .env thì tiến trình đang chạy vẫn dùng token cũ tới lần
restart kế tiếp. File state đọc lại mỗi lần gọi nên có hiệu lực ngay.

Facebook KHÔNG có refresh_token. `fb_exchange_token` đòi một token CÒN SỐNG làm đầu
vào, nên gia hạn chỉ cứu được trường hợp hết hạn tự nhiên; token bị thu hồi hoặc user
đăng xuất (code 190) thì bắt buộc người thật đăng nhập lại — không có đường vòng.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _get_graph_token_url() -> str:
    url = getattr(settings, 'FACEBOOK_GRAPH_OAUTH_URL', '')
    if not url:
        base = getattr(settings, 'FACEBOOK_GRAPH_BASE_URL', '')
        if base:
            url = f"{base.rstrip('/')}/oauth/access_token"
    return url

_get_graph_oauth_url = _get_graph_token_url

STATE_PATH = Path(settings.BASE_DIR) / '.fb_token.json'

# Gia hạn khi còn <= ngưỡng này. Token long-lived sống 60 ngày, cron chạy mỗi ngày,
# nên 7 ngày là thừa chỗ để hỏng vài lượt liên tiếp mà vẫn kịp cứu.
RENEW_THRESHOLD_DAYS = 7


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_token(token: str, expires_in: int) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    STATE_PATH.write_text(json.dumps({'token': token, 'expires_at': expires_at.isoformat()}))


def get_token() -> str:
    """Token đang dùng: ưu tiên bản đã gia hạn, chưa có thì lấy bản gốc trong .env."""
    return _read_state().get('token') or getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')


def get_expires_at():
    raw = _read_state().get('expires_at')
    return datetime.fromisoformat(raw) if raw else None


def refresh_user_token() -> dict:
    """Gia hạn nếu sắp hết hạn. Trả dict {status, message, days_left?}.

    status: ok (còn xa hạn) | refreshed | invalid (token chết) | missing | error
    """
    token = get_token()
    if not token:
        return {'status': 'missing', 'message': 'Chưa cấu hình FACEBOOK_ACCESS_TOKEN'}

    expires_at = get_expires_at()
    if expires_at:
        days_left = (expires_at - datetime.now(timezone.utc)).days
        if days_left > RENEW_THRESHOLD_DAYS:
            return {'status': 'ok', 'message': f'Token còn {days_left} ngày, chưa cần gia hạn', 'days_left': days_left}

    try:
        resp = requests.get(
            _get_graph_token_url(),
            params={
                'grant_type': 'fb_exchange_token',
                'client_id': getattr(settings, 'FACEBOOK_APP_ID', ''),
                'client_secret': getattr(settings, 'FACEBOOK_APP_SECRET', ''),
                'fb_exchange_token': token,
            },
            timeout=int(getattr(settings, 'FACEBOOK_GRAPH_TIMEOUT', 30)),
        )
        data = resp.json()
    except Exception as e:
        return {'status': 'error', 'message': f'Không gọi được Graph API: {e}'}

    new_token = data.get('access_token')
    if not new_token:
        # KHÔNG ghi đè token cũ: giữ lại để còn debug_token ra nguyên nhân. Ghi đè bằng
        # rỗng là mất luôn manh mối, mà cũng chẳng cứu được gì.
        err = (data.get('error') or {}).get('message', 'không rõ nguyên nhân')
        return {'status': 'invalid', 'message': f'Token không gia hạn được — cần đăng nhập lại: {err}'}

    expires_in = data.get('expires_in', 5_184_000)
    save_token(new_token, expires_in)
    days = expires_in // 86_400
    return {'status': 'refreshed', 'message': f'Đã gia hạn token, còn {days} ngày', 'days_left': days}
