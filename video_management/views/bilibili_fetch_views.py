"""Bilibili profile — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi scraper_bilibili_profiles/scraper_bilibili_videos.
AI chỉ gọi TikHub + parse dữ liệu, trả JSON thô cho BE tự lưu.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.tikhub_bilibili import (
    fetch_user_info, fetch_user_videos, parse_bilibili_profile, parse_bilibili_videos,
    search_bilibili_by_keyword, parse_bilibili_search_videos,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_bilibili_profile(request):
    """Fetch profile info + videos, fetch + parse only.

    Body: { "mid": "203680252", "count": 20 }
    """
    data = request.data or {}
    mid = (data.get('mid') or '').strip()
    if not mid:
        return Response({'error': 'mid is required'}, status=400)

    count = int(data.get('count') or 20)

    profile_raw = fetch_user_info(mid)
    parsed_profile = parse_bilibili_profile(profile_raw or {}, mid_fallback=mid) if profile_raw else None

    videos_raw = fetch_user_videos(mid, count=count) if parsed_profile else []
    parsed_videos = parse_bilibili_videos(videos_raw)

    return Response({
        'profile_api_ok': parsed_profile is not None,
        'profile': parsed_profile,
        'videos': parsed_videos,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_bilibili_search(request):
    """Search video theo keyword, fetch + parse only.

    Body: { "keyword": "...", "count": 30 }
    """
    data = request.data or {}
    keyword = (data.get('keyword') or '').strip()
    if not keyword:
        return Response({'error': 'keyword is required'}, status=400)

    count = int(data.get('count') or 30)

    items_raw = search_bilibili_by_keyword(keyword, count=count)
    parsed_videos = parse_bilibili_search_videos(items_raw, search_keyword=keyword)
    return Response({'videos': parsed_videos})
