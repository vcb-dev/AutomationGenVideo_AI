"""Xiaohongshu — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi ScraperXiaohongshuVideo/ScraperXiaohongshuProfile.
AI chỉ gọi TikHub + parse dữ liệu, trả JSON thô cho BE tự lưu.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.tikhub_xiaohongshu import (
    search_xiaohongshu_videos, parse_xiaohongshu_videos,
    fetch_xhs_user_video_notes, parse_xhs_profile_videos, parse_xhs_author,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_xiaohongshu_search(request):
    """Keyword search — fetch + parse only.

    Body: { "keyword": "...", "count": 20, "cursor": {...} }
    `cursor` (optional) — BE truyền lại giá trị `cursor` từ response trước để
    lấy tiếp trang sau, dùng khi dedup phía BE làm giảm số lượng video mới.
    """
    data = request.data or {}
    keyword = (data.get('keyword') or '').strip()
    if not keyword:
        return Response({'error': 'keyword is required'}, status=400)

    count = min(100, max(1, int(data.get('count') or 20)))
    cursor = data.get('cursor') or None

    notes, next_cursor, has_more = search_xiaohongshu_videos(keyword=keyword, count=count, cursor=cursor)
    videos = parse_xiaohongshu_videos(notes)
    return Response({'videos': videos, 'cursor': next_cursor, 'has_more': has_more})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_xiaohongshu_profile_videos(request):
    """Profile flow — fetch video notes theo user_id, fetch + parse only.

    Body: { "user_id": "...", "count": 100 }
    """
    data = request.data or {}
    user_id = (data.get('user_id') or '').strip()
    if not user_id:
        return Response({'error': 'user_id is required'}, status=400)

    count = int(data.get('count') or 100)

    notes = fetch_xhs_user_video_notes(user_id=user_id, count=count)

    author = None
    if notes:
        first_user = (notes[0] or {}).get('user') or {}
        author = parse_xhs_author(first_user)

    videos = parse_xhs_profile_videos(notes)
    return Response({'author': author, 'videos': videos})
