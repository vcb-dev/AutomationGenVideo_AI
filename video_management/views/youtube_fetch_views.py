"""YouTube channel — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi scraper_youtube_profiles/scraper_youtube_shorts.
AI chỉ gọi TikHub + parse dữ liệu, trả JSON thô cho BE tự lưu.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.tikhub_youtube import (
    fetch_channel_info, fetch_channel_shorts, parse_youtube_channel, parse_youtube_shorts,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_youtube_channel(request):
    """Fetch channel info + shorts, fetch + parse only.

    Body: { "channel_id": "UCxxxxxxxx", "count": 20 }
    """
    data = request.data or {}
    channel_id = (data.get('channel_id') or '').strip()
    if not channel_id:
        return Response({'error': 'channel_id is required'}, status=400)

    count = int(data.get('count') or 20)

    channel_raw = fetch_channel_info(channel_id)
    shorts_raw = fetch_channel_shorts(channel_id, count=count)

    parsed_channel = None
    if channel_raw or shorts_raw:
        parsed_channel = parse_youtube_channel(channel_raw or {}, channel_id_fallback=channel_id)

    parsed_shorts = parse_youtube_shorts(shorts_raw)
    return Response({
        'channel_api_ok': channel_raw is not None,
        'profile': parsed_channel,
        'shorts': parsed_shorts,
    })
