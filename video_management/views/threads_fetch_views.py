"""Threads — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc lưu trữ ScraperThreadsProfile / ScraperThreadsPost.
AI chỉ gọi TikHub Threads API + parse dữ liệu, trả JSON thô chuẩn hoá cho BE tự lưu.
"""

import requests
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
from ..services.apify_threads_tag import (
    ThreadsTagConfigError,
    fetch_tag_posts,
    normalize_tag,
    parse_tag_posts,
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


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_threads_search_tag(request):
    """Bài Threads theo TAG CHỦ ĐỀ (bảng tin của tag, qua Apify) — fetch + parse only.

    Body: { "tag": "trang sức", "count": 50 }
    Khác search-top: lấy cả bài gắn tag mà nội dung không chứa chữ đó, không lọc theo từ khoá.
    """
    data = request.data or {}
    tag = normalize_tag(data.get('tag') or '')
    if not tag:
        return Response({'error': 'tag is required'}, status=400)

    try:
        count = int(data.get('count') or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return Response({'error': 'count phải là số nguyên dương'}, status=400)

    try:
        raw = fetch_tag_posts(tag, count)
    except ThreadsTagConfigError as e:
        return Response({'error': str(e)}, status=400)
    except requests.RequestException as e:
        return Response({'error': f'Apify không trả được bài của tag "{tag}": {e}'}, status=502)

    return Response({
        'tag': tag,
        'posts': parse_tag_posts(raw, tag),
    })
