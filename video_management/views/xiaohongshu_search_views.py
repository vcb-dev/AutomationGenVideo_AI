"""Xiaohongshu video search + user profile views — dùng TikHub."""

import logging
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models_scraper import XiaohongshuVideo, XiaohongshuProfile

logger = logging.getLogger(__name__)

_SORT_FIELDS = {
    'likes': '-liked_count',
    'date': '-date_posted',
    'collects': '-collected_count',
    'comments': '-comments_count',
}


def _parse_int(val, default=None):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def search_xiaohongshu_notes(request):
    """Tìm kiếm video notes trên Xiaohongshu qua TikHub, lưu vào DB.

    POST /api/xiaohongshu/search/
    Body: { "keyword": "美食推荐", "num_of_posts": 20 }
    """
    keyword = (request.data.get('keyword') or '').strip()
    if not keyword:
        return Response({'error': 'keyword is required'}, status=400)

    num = min(max(_parse_int(request.data.get('num_of_posts'), 20), 1), 100)

    try:
        from ..services.tikhub_xiaohongshu import search_xiaohongshu_videos, ingest_xiaohongshu_videos
        notes = search_xiaohongshu_videos(keyword=keyword, count=num)

        if not notes:
            return Response({
                'status': 'ok',
                'message': f'Không tìm thấy video nào cho "{keyword}".',
                'created': 0, 'updated': 0,
            })

        result = ingest_xiaohongshu_videos(notes, keyword=keyword)
        return Response({
            'status': 'ok',
            'message': f'Đã tìm thấy {result["created"]} video mới cho "{keyword}".',
            **result,
        })

    except Exception as e:
        logger.error(f'[XHS] search failed: {e}', exc_info=True)
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_xiaohongshu_videos(request):
    """Danh sách Xiaohongshu videos đã lưu trong DB.

    GET /api/scraper/xiaohongshu/videos/?page=1&page_size=24&q=美食&sort=likes
        &min_likes=100&date_from=2026-01-01&date_to=2026-06-30&keyword=美食推荐
    """
    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 24)))
    q = params.get('q', '').strip()
    sort = _SORT_FIELDS.get(params.get('sort', 'likes'), '-liked_count')
    min_likes = _parse_int(params.get('min_likes'))
    date_from = params.get('date_from', '').strip()
    date_to = params.get('date_to', '').strip()
    keyword_filter = params.get('keyword', '').strip()

    qs = XiaohongshuVideo.objects.all()

    if q:
        from django.db.models import Q
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(author_name__icontains=q))
    if keyword_filter:
        qs = qs.filter(keywords__contains=[keyword_filter])
    if min_likes is not None:
        qs = qs.filter(liked_count__gte=min_likes)
    if date_from:
        qs = qs.filter(date_posted__date__gte=date_from)
    if date_to:
        qs = qs.filter(date_posted__date__lte=date_to)

    qs = qs.order_by(sort)
    total = qs.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page_num - 1) * page_size
    page_qs = qs[start:start + page_size]

    videos = [
        {
            'id': v.id,
            'note_id': v.note_id,
            'url': v.url,
            'title': v.title,
            'description': v.description,
            'thumbnail_url': v.thumbnail_drive_url or v.thumbnail_url or '',
            'author_id': v.author_id,
            'author_name': v.author_name,
            'author_avatar': v.author_avatar or '',
            'duration_seconds': v.duration_seconds,
            'liked_count': v.liked_count,
            'collected_count': v.collected_count,
            'comments_count': v.comments_count,
            'shared_count': v.shared_count,
            'keywords': v.keywords,
            'date_posted': v.date_posted,
        }
        for v in page_qs
    ]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'videos': videos,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def xiaohongshu_keyword_suggest(request):
    """Gợi ý keywords đã dùng từ DB.

    GET /api/scraper/xiaohongshu/keywords/suggest/?q=美食
    """
    q = request.query_params.get('q', '').strip()

    qs = XiaohongshuVideo.objects.values_list('keywords', flat=True).distinct()

    keyword_counts: dict = {}
    for kw_list in qs:
        for kw in (kw_list or []):
            if not q or q.lower() in kw.lower():
                keyword_counts[kw] = keyword_counts.get(kw, 0) + 1

    suggestions = sorted(
        [{'keyword': k, 'count': c} for k, c in keyword_counts.items()],
        key=lambda x: -x['count'],
    )[:20]

    return Response(suggestions)


# ═══════════════════════════════════════════════════════════
#  XIAOHONGSHU PROFILE VIEWS
# ═══════════════════════════════════════════════════════════

def _profile_dict(p: XiaohongshuProfile, include_video_count: bool = False) -> dict:
    d = {
        'id': p.id,
        'user_id': p.user_id,
        'nickname': p.nickname,
        'avatar_url': p.avatar_url or '',
        'is_verified': p.is_verified,
        'is_tracked': p.is_tracked,
        'is_bookmarked': p.is_bookmarked,
        'is_owned': p.is_owned,
        'is_initial_scraped': p.is_initial_scraped,
        'last_scraped_at': p.last_scraped_at,
        'scraping_status': p.scraping_status,
        'scrape_error': p.scrape_error,
        'created_at': p.created_at,
    }
    if include_video_count:
        d['videos_count'] = p.videos.count()
    return d


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def xhs_profile_scrape(request):
    """Thêm profile mới + trigger scrape.

    POST /api/scraper/xiaohongshu/profiles/scrape/
    Body: { "user_id": "61b46d790000000010008153", "num_of_posts": 100 }
    """
    user_id = (request.data.get('user_id') or '').strip()
    if not user_id:
        return Response({'error': 'user_id is required'}, status=400)

    is_owned = bool(request.data.get('is_owned', False))
    num = min(max(_parse_int(request.data.get('num_of_posts'), 600), 1), 600)

    profile, created = XiaohongshuProfile.objects.get_or_create(
        user_id=user_id,
        defaults={'scraping_status': 'idle', 'is_owned': is_owned},
    )

    if profile.scraping_status == 'processing':
        return Response({'error': 'Profile đang được cào, vui lòng đợi'}, status=400)

    update_fields = ['scraping_status', 'scrape_error', 'updated_at']
    if is_owned and not profile.is_owned:
        profile.is_owned = True
        update_fields.append('is_owned')

    from ..tasks import scrape_xhs_profile_task
    scrape_xhs_profile_task.delay(profile.id, num_of_posts=num)

    profile.scraping_status = 'processing'
    profile.scrape_error = None
    profile.save(update_fields=update_fields)

    return Response({
        'status': 'ok',
        'message': f'Đang cào video từ user {user_id}...',
        'profile': _profile_dict(profile),
        'created': created,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def xhs_profiles_list(request):
    """Danh sách XHS profiles.

    GET /api/scraper/xiaohongshu/profiles/?q=&bookmarked=&tracked=
    """
    params = request.query_params
    q = params.get('q', '').strip()
    bookmarked = params.get('bookmarked', '')
    tracked = params.get('tracked', '')
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(50, max(1, _parse_int(params.get('page_size'), 20)))

    is_owned_param = params.get('is_owned', '').strip()

    qs = XiaohongshuProfile.objects.all()
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(nickname__icontains=q) | Q(user_id__icontains=q))
    if bookmarked == 'true':
        qs = qs.filter(is_bookmarked=True)
    if tracked == 'true':
        qs = qs.filter(is_tracked=True)
    if is_owned_param == 'true':
        qs = qs.filter(is_owned=True)
    elif is_owned_param == 'false':
        qs = qs.filter(is_owned=False)

    qs = qs.order_by('-created_at')
    total = qs.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page_num - 1) * page_size
    page_qs = qs[start:start + page_size]

    from django.db.models import Count
    profiles = [_profile_dict(p, include_video_count=True) for p in page_qs]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'profiles': profiles,
    })


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def xhs_profile_detail(request, profile_id: int):
    """GET profile detail / PATCH để toggle tracked/bookmarked.

    PATCH body: { "is_tracked": true } | { "is_bookmarked": true }
    """
    try:
        profile = XiaohongshuProfile.objects.get(id=profile_id)
    except XiaohongshuProfile.DoesNotExist:
        return Response({'error': 'Profile not found'}, status=404)

    if request.method == 'PATCH':
        changed = []
        for field in ('is_tracked', 'is_bookmarked'):
            val = request.data.get(field)
            if val is not None:
                setattr(profile, field, bool(val))
                changed.append(field)
        if changed:
            changed.append('updated_at')
            profile.save(update_fields=changed)

    return Response(_profile_dict(profile, include_video_count=True))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def xhs_profile_videos(request, profile_id: int):
    """Videos của một XHS profile (paginated).

    GET /api/scraper/xiaohongshu/profiles/<id>/videos/?page=1&sort=likes
    """
    try:
        profile = XiaohongshuProfile.objects.get(id=profile_id)
    except XiaohongshuProfile.DoesNotExist:
        return Response({'error': 'Profile not found'}, status=404)

    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 24)))
    sort = _SORT_FIELDS.get(params.get('sort', 'likes'), '-liked_count')

    q = params.get('q', '').strip()
    min_likes = _parse_int(params.get('min_likes'))

    qs = XiaohongshuVideo.objects.filter(profile=profile)
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
    if min_likes is not None:
        qs = qs.filter(liked_count__gte=min_likes)
    qs = qs.order_by(sort)
    total = qs.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page_num - 1) * page_size
    page_qs = qs[start:start + page_size]

    videos = [
        {
            'id': v.id,
            'note_id': v.note_id,
            'url': v.url,
            'title': v.title,
            'description': v.description,
            'thumbnail_url': v.thumbnail_drive_url or v.thumbnail_url or '',
            'author_id': v.author_id,
            'author_name': v.author_name,
            'author_avatar': v.author_avatar or '',
            'duration_seconds': v.duration_seconds,
            'liked_count': v.liked_count,
            'collected_count': v.collected_count,
            'comments_count': v.comments_count,
            'shared_count': v.shared_count,
            'keywords': v.keywords,
            'date_posted': v.date_posted,
        }
        for v in page_qs
    ]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'profile': _profile_dict(profile),
        'videos': videos,
    })
