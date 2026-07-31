"""Endpoint lấy chi tiết 1 video theo (platform, video_id) — phục vụ luồng đề xuất video.

BE gọi vào đây (đúng quy tắc FE → BE → AI), không cho FE/extension gọi thẳng: mỗi lượt gọi
là một lượt tính phí TikHub, phải đi qua BE để còn kiểm soát quyền.
"""

import logging
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from ..services.tikhub_video_detail import (
    fetch_video_detail,
    SUPPORTED_PLATFORMS,
)

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([AllowAny])
def get_video_detail(request):
    """
    Body: {"platform": "douyin", "video_id": "7659675415467902374", "video_url": "https://..."}

    Luôn trả HTTP 200 kèm success=false khi không lấy được — đây là bước làm giàu dữ liệu,
    BE phải đi tiếp được chứ không được vỡ luồng đề xuất chỉ vì thiếu số liệu.
    """
    platform = str(request.data.get('platform') or '').lower().strip()
    video_id = str(request.data.get('video_id') or '').strip()
    video_url = str(request.data.get('video_url') or '').strip()

    if not platform:
        return Response({'success': False, 'error': 'Thiếu platform.'})
    if platform not in SUPPORTED_PLATFORMS:
        return Response({
            'success': False,
            'error': f'Nền tảng "{platform}" chưa hỗ trợ lấy chi tiết video.',
            'supported': SUPPORTED_PLATFORMS,
        })
    if not video_id and not video_url:
        return Response({'success': False, 'error': 'Thiếu video_id hoặc video_url.'})

    try:
        data = fetch_video_detail(platform, video_id, video_url)
    except Exception as e:  # noqa: BLE001 — chặn mọi lỗi, không để vỡ luồng đề xuất
        logger.exception(f"[VIDEO-DETAIL] Loi khong luong truoc ({platform}): {e}")
        return Response({'success': False, 'error': 'Lỗi khi lấy chi tiết video.'})

    if not data:
        return Response({'success': False, 'error': 'Không lấy được chi tiết video.'})

    return Response({'success': True, 'data': data})
