"""TikTok — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi ScraperTikTokVideo/ScraperTikTokProfile/
ScraperTikTokProfileVideo. AI chỉ gọi TikHub + parse dữ liệu, trả JSON thô cho BE tự lưu.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.tikhub_tiktok import search_tiktok_by_keyword, parse_tiktok_videos
from ..services.tikhub_tiktok_profile import fetch_user_posts, parse_tiktok_author, parse_tiktok_profile_videos


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_tiktok_search(request):
    """Keyword search — fetch + parse only.

    Body: { "keyword": "...", "count": 30, "region": "VN" }
    """
    data = request.data or {}
    keyword = (data.get('keyword') or '').strip()
    if not keyword:
        return Response({'error': 'keyword is required'}, status=400)

    count = min(200, max(1, int(data.get('count') or 30)))
    region = data.get('region', 'VN')

    videos = search_tiktok_by_keyword(keyword=keyword, count=count, region=region)
    parsed = parse_tiktok_videos(videos, search_keyword=keyword)
    return Response({'videos': parsed})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_tiktok_profile_posts(request):
    """Profile flow — fetch posts theo username/sec_uid, fetch + parse only.

    Body: { "username": "...", "sec_uid": "...", "count": 20, "days": null }
    """
    data = request.data or {}
    username = (data.get('username') or '').strip()
    sec_uid = (data.get('sec_uid') or '').strip()
    if not username and not sec_uid:
        return Response({'error': 'username or sec_uid is required'}, status=400)

    count = int(data.get('count') or 20)
    days = data.get('days')
    days = int(days) if days else None

    items = fetch_user_posts(username, count=count, days=days, sec_uid=sec_uid)

    author = None
    if items:
        first_author = (items[0] or {}).get('author') or {}
        author = parse_tiktok_author(first_author)

    resolved_username = (author.get('username') if author else '') or username
    videos = parse_tiktok_profile_videos(items, username=resolved_username)

    return Response({'author': author, 'videos': videos})
