"""Kuaishou profile — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi scraper_kuaishou_profiles/scraper_kuaishou_videos.
AI chỉ gọi TikHub + parse dữ liệu, trả JSON thô cho BE tự lưu.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.tikhub_kuaishou import (
    fetch_one_user_v2, fetch_user_hot_post, parse_kuaishou_profile, parse_kuaishou_videos,
    search_kuaishou_by_keyword, parse_kuaishou_search_videos,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_kuaishou_profile(request):
    """Fetch profile info + videos, fetch + parse only.

    Body: { "eid": "3xz63mn6fngqtiq", "count": 20 }

    2 bước tuần tự (không song song được): fetch_one_user_v2(eid) trước để
    resolve numeric user_id, rồi mới gọi fetch_user_hot_post(user_id) — vì
    fetch_user_hot_post không chấp nhận eid.
    """
    data = request.data or {}
    eid = (data.get('eid') or '').strip()
    if not eid:
        return Response({'error': 'eid is required'}, status=400)

    count = int(data.get('count') or 20)

    profile_raw = fetch_one_user_v2(eid)
    parsed_profile = parse_kuaishou_profile(profile_raw or {}, eid) if profile_raw else None

    videos_raw = fetch_user_hot_post(parsed_profile['user_id'], count=count) if parsed_profile else []
    parsed_videos = parse_kuaishou_videos(videos_raw)

    return Response({
        'profile_api_ok': parsed_profile is not None,
        'profile': parsed_profile,
        'videos': parsed_videos,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_kuaishou_search(request):
    """Search video theo keyword, fetch + parse only.

    Body: { "keyword": "...", "count": 30, "cursor": "..." }
    `cursor` (optional) — BE truyền lại giá trị `cursor` từ response trước để
    lấy tiếp trang sau, dùng khi dedup phía BE làm giảm số lượng video mới.
    """
    data = request.data or {}
    keyword = (data.get('keyword') or '').strip()
    if not keyword:
        return Response({'error': 'keyword is required'}, status=400)

    count = int(data.get('count') or 30)
    cursor = data.get('cursor') or None

    items_raw, next_cursor, has_more = search_kuaishou_by_keyword(keyword, count=count, cursor=cursor)
    parsed_videos = parse_kuaishou_search_videos(items_raw, search_keyword=keyword)
    return Response({'videos': parsed_videos, 'cursor': next_cursor, 'has_more': has_more})
