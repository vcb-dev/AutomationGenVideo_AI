"""Douyin — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi ScraperDouyinVideo/ScraperDouyinProfile.
AI chỉ gọi TikHub + parse dữ liệu, trả JSON thô cho BE tự lưu.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.tikhub_douyin import (
    fetch_douyin_videos, fetch_douyin_user_videos, parse_douyin_videos, parse_douyin_author,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_douyin_search(request):
    """Keyword search — fetch + parse only.

    Body: { "keyword": "...", "count": 30 }
    """
    data = request.data or {}
    keyword = (data.get('keyword') or '').strip()
    if not keyword:
        return Response({'error': 'keyword is required'}, status=400)

    count = min(200, max(1, int(data.get('count') or 30)))

    videos = fetch_douyin_videos(keyword=keyword, count=count)
    parsed = parse_douyin_videos(videos, search_keyword=keyword)
    return Response({'videos': parsed})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_douyin_profile_videos(request):
    """Profile flow — fetch videos theo sec_user_id, fetch + parse only.

    Body: { "sec_user_id": "...", "count": 20 }
    """
    data = request.data or {}
    sec_user_id = (data.get('sec_user_id') or '').strip()
    if not sec_user_id:
        return Response({'error': 'sec_user_id is required'}, status=400)

    count = int(data.get('count') or 20)

    items = fetch_douyin_user_videos(sec_user_id=sec_user_id, count=count)

    author = None
    if items:
        first_author = (items[0] or {}).get('author') or {}
        author = parse_douyin_author(first_author)

    # search_keyword để '' — BE tự gán '@{username}' bằng username đã resolve
    # (ưu tiên DB hiện có, fallback author vừa fetch), tránh AI đoán sai label.
    videos = parse_douyin_videos(items, search_keyword='')
    return Response({'author': author, 'videos': videos})
