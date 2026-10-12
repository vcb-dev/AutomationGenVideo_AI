"""Phân biệt "TikHub từ chối khoá API" với "kênh không có video".

Vì sao cần tách riêng: mọi service tikhub_* trước đây bắt lỗi HTTP rồi trả `[]`/`None`,
nên hai tình huống hoàn toàn khác nhau cùng đi ra một kết quả. BE nhận danh sách rỗng
và kết luận "kênh không tồn tại", XOÁ profile vừa tạo, còn người dùng đọc được thông
báo hoàn toàn sai lệch. Sự cố thật ngày 12/08/2026: token TikHub hết hạn, cả 7 nền tảng
báo "không tìm thấy kênh" suốt nhiều ngày — không ai nghĩ tới khoá API.

Ranh giới ném / không ném:

  401 / 403  khoá sai, hết hạn, bị thu hồi
  429        hết hạn mức
             → NÉM. Thử lại bao nhiêu lần cũng vậy, phải có người vào gia hạn.

  408 / 5xx / timeout / đứt kết nối
             → KHÔNG ném, giữ nguyên nết cũ (trả về những gì đã lấy được). Đây là
               lỗi tự khỏi; ném ở đây sẽ biến một cú mạng chập thành sập cả lượt cào.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 429 nằm chung nhóm với 401/403 vì cùng một cách xử lý: người vận hành phải ra tay
# (nâng gói hoặc chờ reset hạn mức), không phải thứ thử lại vài giây là qua.
AUTH_STATUS = frozenset({401, 403, 429})


class TikHubAuthError(RuntimeError):
    """TikHub từ chối vì khoá API — KHÔNG phải vì kênh rỗng hay không tồn tại."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _thong_diep_upstream(resp) -> str:
    """Bóc câu giải thích của TikHub. Thân lỗi có dạng {"detail": {"message": "..."}}."""
    try:
        body = resp.json() or {}
    except Exception:
        return (getattr(resp, 'text', '') or '')[:200]

    detail = body.get('detail')
    if isinstance(detail, dict):
        return str(detail.get('message') or detail)[:200]
    return str(detail or body.get('message') or body)[:200]


def raise_if_auth_error(resp, nguon: str) -> None:
    """Ném TikHubAuthError nếu `resp` là 401/403/429. Các mã khác: không làm gì."""
    status = getattr(resp, 'status_code', None)
    if status not in AUTH_STATUS:
        return

    ly_do = _thong_diep_upstream(resp)
    logger.error(f'[TIKHUB-AUTH] {nguon} — HTTP {status}: {ly_do}')
    raise TikHubAuthError(
        f'TikHub từ chối yêu cầu ({status}) ở {nguon}: {ly_do}. '
        f'Khoá TIKHUB_API_KEY cần được gia hạn hoặc nâng hạn mức — '
        f'đây KHÔNG phải lỗi của kênh vừa nhập.',
        status=status,
    )


def raise_if_auth_exception(exc, nguon: str) -> None:
    """Bản dùng trong `except requests.RequestException`.

    Timeout/ConnectionError không có `.response` nên hàm này im lặng bỏ qua, đúng như
    mong muốn: chỉ lỗi có phản hồi thật từ TikHub mới xét tới.
    """
    raise_if_auth_error(getattr(exc, 'response', None), nguon)
