"""Reddit — fetch-only endpoints (không đụng DB). BE gọi để lấy bài đã chuẩn hoá rồi tự lưu."""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.tikhub_reddit import (
    SEARCH_SORTS,
    TIME_RANGES,
    RedditConfigError,
    fetch_subreddit_info,
    normalize_subreddit_name,
    parse_reddit_posts,
    parse_subreddit,
    search_posts,
)


def _read_common(data: dict):
    """(sort, time_range, count, lỗi). Không có giá trị mặc định — BE luôn gửi đủ."""
    sort = str(data.get('sort') or '').upper()
    time_range = str(data.get('time_range') or '').lower()
    try:
        count = int(data.get('count') or 0)
    except (TypeError, ValueError):
        count = 0
    if sort not in SEARCH_SORTS:
        return None, None, None, f'sort phải là một trong {", ".join(SEARCH_SORTS)}'
    if time_range not in TIME_RANGES:
        return None, None, None, f'time_range phải là một trong {", ".join(TIME_RANGES)}'
    if count <= 0:
        return None, None, None, 'count phải là số nguyên dương'
    return sort, time_range, count, None


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_reddit_search(request):
    """Bài Reddit theo từ khoá. Body: {query, sort, time_range, count}."""
    data = request.data or {}
    query = str(data.get('query') or '').strip()
    if not query:
        return Response({'error': 'query is required'}, status=400)
    sort, time_range, count, err = _read_common(data)
    if err:
        return Response({'error': err}, status=400)

    try:
        raw = search_posts(query, sort, time_range, count)
    except RedditConfigError as e:
        return Response({'error': str(e)}, status=400)
    return Response({'query': query, 'posts': parse_reddit_posts(raw, keyword=query)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_reddit_subreddit(request):
    """Bài của một cộng đồng. Body: {subreddit: "r/jewelry" | link | tên, sort, time_range, count}."""
    data = request.data or {}
    name = normalize_subreddit_name(str(data.get('subreddit') or ''))
    if not name:
        return Response({'error': 'Tên subreddit không hợp lệ (vd: r/jewelry hoặc link reddit.com/r/jewelry)'}, status=400)
    sort, time_range, count, err = _read_common(data)
    if err:
        return Response({'error': err}, status=400)

    try:
        raw = search_posts(f'subreddit:{name}', sort, time_range, count)
        # Bài crosspost có thể mang subreddit khác — chỉ giữ bài của đúng cộng đồng này.
        posts = [p for p in parse_reddit_posts(raw) if p['subreddit_name'] == name]
        info = next((p['subreddit'] for p in posts if p.get('subreddit')), None)
        if info is None:
            info = parse_subreddit(fetch_subreddit_info(name))
    except RedditConfigError as e:
        return Response({'error': str(e)}, status=400)

    if info is None:
        return Response({'error': f'Không tìm thấy cộng đồng r/{name} trên Reddit'}, status=404)
    return Response({'subreddit': info, 'posts': posts})
