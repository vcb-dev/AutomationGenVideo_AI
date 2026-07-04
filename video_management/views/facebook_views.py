from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import ManagedFacebookPage, OwnedVideoContent
from ..services.facebook_fetcher import FacebookFetcher


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def facebook_import(request):
    """Import danh sách Pages chính chủ từ Facebook Graph API vào DB.

    Đây là thao tác nhẹ (chỉ lấy metadata page, không cào video).
    Cào video sẽ do Cron Job xử lý tự động sau.
    """
    data = request.data or {}
    user_token = data.get('user_access_token')

    fetcher = FacebookFetcher()

    try:
        counts = fetcher.import_my_managed_pages(user_access_token=user_token)
        return Response({
            'status': 'ok',
            'created': counts['created'],
            'updated': counts['updated'],
            'message': f"Đã đồng bộ xong danh sách Page (Thêm mới: {counts['created']}, Cập nhật: {counts['updated']})"
        })
    except Exception as e:
        return Response({
            'status': 'error',
            'message': str(e)
        }, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def facebook_sync(request):
    """Trigger cào video cho 1 page cụ thể (dispatch sang Celery worker).

    API trả về ngay lập tức — worker xử lý ngầm.
    """
    data = request.data or {}
    page_id = data.get('page_id')

    if not page_id:
        return Response({'error': 'page_id is required'}, status=400)

    try:
        page = ManagedFacebookPage.objects.get(page_id=str(page_id))
    except ManagedFacebookPage.DoesNotExist:
        return Response({'error': f'Page {page_id} not found'}, status=404)

    if page.is_scraping:
        return Response({
            'status': 'ok',
            'message': f'Page {page.name} đang được cào bởi worker. Vui lòng đợi.',
            'is_scraping': True,
        })

    from ..tasks import scrape_single_facebook_page_task
    scrape_single_facebook_page_task.delay(page_id)

    return Response({
        'status': 'ok',
        'message': f'Đã gửi yêu cầu cào video cho {page.name}. Dữ liệu sẽ cập nhật trong vài phút.',
        'is_scraping': True,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def facebook_backfill(request):
    """Trigger backfill (cào lượt đầu) cho 1 page — dispatch sang Celery."""
    data = request.data or {}
    page_id = data.get('page_id')

    if not page_id:
        return Response({'error': 'page_id is required'}, status=400)

    try:
        page = ManagedFacebookPage.objects.get(page_id=str(page_id))
    except ManagedFacebookPage.DoesNotExist:
        return Response({'error': f'Page {page_id} not found'}, status=404)

    if page.is_scraping:
        return Response({
            'status': 'ok',
            'message': f'Page {page.name} đang được xử lý. Vui lòng đợi.',
            'is_scraping': True,
        })

    from ..tasks import backfill_single_page_task
    backfill_single_page_task.delay(page_id)

    return Response({
        'status': 'ok',
        'message': f'Đã gửi yêu cầu cào lượt đầu cho {page.name}. Quá trình sẽ mất vài phút.',
        'is_scraping': True,
    })


import math
from unidecode import unidecode


def _unaccent_match(text: str, query: str) -> bool:
    """Vietnamese diacritics-insensitive search. 'nguyen' matches 'Nguyễn'."""
    if not text or not query:
        return False
    return unidecode(query).lower() in unidecode(text).lower()


def _parse_int(value, default=None):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_managed_pages(request):
    """Danh sách pages với pagination, filter, search (unaccent).

    Query params:
    - page (int, default 1)
    - page_size (int, default 20, max 100)
    - search (string — tìm theo tên, không dấu cũng khớp)
    - status (string: "active" | "inactive")
    - min_likes (int)
    - min_followers (int)
    """
    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 20)))
    search = params.get('search', '').strip()
    filter_status = params.get('status', '').strip().lower()
    min_likes = _parse_int(params.get('min_likes'))
    min_followers = _parse_int(params.get('min_followers'))

    qs = ManagedFacebookPage.objects.annotate(video_count_ann=Count('videos'))

    # SQL filters
    if filter_status == 'active':
        qs = qs.filter(is_active=True)
    elif filter_status == 'inactive':
        qs = qs.filter(is_active=False)

    if min_likes is not None:
        qs = qs.filter(likes_count__gte=min_likes)

    if min_followers is not None:
        qs = qs.filter(followers_count__gte=min_followers)

    qs = qs.order_by('-video_count_ann', '-last_scraped_at', 'name')

    # Unaccent search (Python-side, dataset nhỏ < 100 pages)
    if search:
        all_pages = list(qs)
        all_pages = [p for p in all_pages if _unaccent_match(p.name, search)]
    else:
        all_pages = list(qs)

    # Pagination
    total = len(all_pages)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = all_pages[start:start + page_size]

    results = [
        {
            'page_id': p.page_id,
            'name': p.name,
            'username': p.username,
            'category': p.category,
            'avatar_url': p.avatar_url,
            'followers_count': p.followers_count,
            'likes_count': p.likes_count,
            'is_active': p.is_active,
            'is_scraping': p.is_scraping,
            'is_backfilled': p.is_backfilled,
            'last_synced_at': p.last_synced_at,
            'last_scraped_at': p.last_scraped_at,
            'scrape_error': p.scrape_error,
            'video_count': p.video_count_ann,
            'created_at': p.created_at,
            'updated_at': p.updated_at,
        }
        for p in paginated
    ]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'pages': results,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_synced_videos(request, page_id: str):
    """Videos của 1 page với pagination, filter, search (unaccent).

    Query params:
    - page (int, default 1)
    - page_size (int, default 20, max 100)
    - search (string — tìm trong caption/hashtag, không dấu khớp có dấu)
    - min_views (int, default 10000)
    - min_likes (int)
    - hashtag_category (string: "a1"|"a2"|"a3"|"a4"|"a5")
    - date_from (ISO date: YYYY-MM-DD)
    - date_to (ISO date: YYYY-MM-DD)
    """
    page_obj = get_object_or_404(ManagedFacebookPage, page_id=page_id)

    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 20)))
    search = params.get('search', '').strip()
    min_views = _parse_int(params.get('min_views'), 10000)
    min_likes = _parse_int(params.get('min_likes'))
    hashtag_cat = params.get('hashtag_category', '').strip().lower()
    date_from = params.get('date_from', '').strip()
    date_to = params.get('date_to', '').strip()

    qs = page_obj.videos.all()

    # SQL filters
    if min_views is not None:
        qs = qs.filter(view_count__gte=min_views)

    if min_likes is not None:
        qs = qs.filter(like_count__gte=min_likes)

    if hashtag_cat:
        qs = qs.filter(caption__icontains=f'#{hashtag_cat}')

    if date_from:
        qs = qs.filter(published_at__date__gte=date_from)

    if date_to:
        qs = qs.filter(published_at__date__lte=date_to)

    qs = qs.order_by('-view_count')

    # Unaccent search on caption (Python-side after SQL filters reduce the set)
    if search:
        filtered_videos = [v for v in qs if _unaccent_match(v.caption or '', search)]
    else:
        filtered_videos = list(qs)

    # Pagination
    total = len(filtered_videos)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = filtered_videos[start:start + page_size]

    videos = [
        {
            'post_id': v.post_id,
            'caption': v.caption,
            'published_at': v.published_at,
            'permalink_url': v.permalink_url,
            'thumbnail_url': v.thumbnail_url,
            'video_url': v.video_url,
            'view_count': v.view_count,
            'like_count': v.like_count,
            'comment_count': v.comment_count,
            'share_count': v.share_count,
            'reach_count': v.reach_count,
            'link_clicks': v.link_clicks,
            'last_updated_at': v.last_updated_at,
        }
        for v in paginated
    ]

    return Response({
        'status': 'ok',
        'page_info': {
            'page_id': page_obj.page_id,
            'name': page_obj.name,
            'username': page_obj.username,
            'avatar_url': page_obj.avatar_url,
            'category': page_obj.category,
            'followers_count': page_obj.followers_count,
            'likes_count': page_obj.likes_count,
            'is_scraping': page_obj.is_scraping,
            'last_scraped_at': page_obj.last_scraped_at,
        },
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'videos': videos,
    })
