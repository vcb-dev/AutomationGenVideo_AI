"""Bộ xử lý ngoại lệ chung của DRF.

Đặt ở một chỗ thay vì try/except trong từng view: các view fetch-only nằm rải khắp
`video_management/views/`, thêm tay từng chỗ thì chắc chắn sót — mà chỗ sót chính là
chỗ sẽ báo sai cho người dùng.
"""

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from video_management.services.tikhub_errors import TikHubAuthError


def xu_ly_ngoai_le(exc, context):
    """Trả Response cho ngoại lệ tự nhận; còn lại nhường DRF xử lý như cũ.

    TikHubAuthError → 502 Bad Gateway: hỏng nằm ở nhà cung cấp phía sau, không phải ở
    yêu cầu người dùng gửi lên. Chọn 502 thay vì 500 để BE phân biệt được "AI service
    sập" với "TikHub từ chối" — và để 4xx vẫn giữ nguyên nghĩa "người dùng nhập sai".
    """
    if isinstance(exc, TikHubAuthError):
        return Response({'error': str(exc), 'tikhub_status': exc.status}, status=502)

    return drf_exception_handler(exc, context)
