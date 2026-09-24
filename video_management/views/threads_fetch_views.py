"""Threads — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc lưu trữ ScraperThreadsProfile / ScraperThreadsPost.
AI chỉ gọi TikHub Threads API + parse dữ liệu, trả JSON thô chuẩn hoá cho BE tự lưu.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.tikhub_threads import (
    fetch_user_info,
    parse_threads_user_info,
    fetch_threads_posts,
    search_top_threads,
    parse_threads_posts,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_threads_profile_posts(request):
    """Fetch profile info + posts theo username Threads, fetch + parse only.

    Body: { "username": "...", "count": 50 }
    """
    data = request.data or {}
    username = (data.get('username') or '').strip().lstrip('@')
    if not username:
        return Response({'error': 'username is required'}, status=400)

    count = int(data.get('count') or 50)

    user_info = fetch_user_info(username)
    full_profile = parse_threads_user_info(username, user_info) if user_info else None

    posts = []
    if full_profile and full_profile.get('threads_user_id'):
        raw_posts = fetch_threads_posts(user_id=full_profile['threads_user_id'], count=count)
        posts = parse_threads_posts(raw_posts, default_username=username)

    return Response({
        'full_profile': full_profile,
        'posts': posts,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_threads_search_top(request):
    """Tìm kiếm nội dung Threads phổ biến/trending theo từ khoá.

    Body: { "query": "...", "count": 50 }
    """
    data = request.data or {}
    query = (data.get('query') or '').strip()
    if not query:
        return Response({'error': 'query is required'}, status=400)

    count = int(data.get('count') or 50)
    raw_posts = search_top_threads(query=query, count=count)
    posts = parse_threads_posts(raw_posts, query=query)

    return Response({
        'query': query,
        'posts': posts[:count],
    })
