"""Endpoint trả link phát trực tiếp cho BE dùng làm trung gian phát video.

BE gọi vào đây (FE → BE → AI), FE không gọi thẳng: mỗi lượt là một lượt TikHub tính phí.
"""

import logging
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from ..services.tikhub_play_url import fetch_play_url, SUPPORTED_PLATFORMS, EMBED_PLATFORMS, HET_TIEN

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([AllowAny])
def get_play_url(request):
    """Body: {"platform": "douyin", "video_id": "...", "video_url": "https://..."}"""
    platform = str(request.data.get('platform') or '').lower().strip()
    video_id = str(request.data.get('video_id') or '').strip()
    video_url = str(request.data.get('video_url') or '').strip()
    # BE bat buoc lay moi khi vua gap 403 vi link cu het han — xem tikhub_cache.goi_co_dem.
    lam_moi = bool(request.data.get('force_refresh'))

    if platform in EMBED_PLATFORMS:
        # Nền tảng này có mã nhúng chính chủ — FE nhúng iframe, không tốn lượt gọi nào.
        return Response({'success': False, 'reason': 'use_embed', 'error': f'{platform} dùng mã nhúng chính chủ.'})
    if platform not in SUPPORTED_PLATFORMS:
        return Response({'success': False, 'reason': 'unsupported', 'supported': SUPPORTED_PLATFORMS})
    if not video_id and not video_url:
        return Response({'success': False, 'reason': 'missing_id', 'error': 'Thiếu video_id hoặc video_url.'})

    try:
        url = fetch_play_url(platform, video_id, video_url, lam_moi=lam_moi)
    except Exception as e:  # noqa: BLE001
        logger.exception(f'[PLAY-URL] loi khong luong truoc ({platform}): {e}')
        return Response({'success': False, 'reason': 'error'})

    if url == HET_TIEN:
        # Phan biet ro voi 'not_found': het tien thi ca 4 nen tang chet cung luc va cach
        # chua la nap tien chu khong phai sua code. Gop chung se mat rat nhieu thoi gian
        # do loi nham (da mat that mot lan).
        return Response({
            'success': False,
            'reason': 'no_credit',
            'error': 'Tài khoản TikHub đã hết số dư — cần nạp thêm để lấy được link phát.',
        })
    if not url:
        return Response({'success': False, 'reason': 'not_found'})
    return Response({'success': True, 'play_url': url})
