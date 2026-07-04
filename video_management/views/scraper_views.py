"""API Views for Scraper system: keyword search, fanpage discovery, reel search."""

import math
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone
from unidecode import unidecode

from ..models_scraper import (
    SearchKeyword, ScrapedFanpage, FacebookReel,
    TikTokVideo, TikTokProfileVideo, TikTokProfile,
    InstagramProfile, InstagramReel,
    DouyinVideo, DouyinProfile,
    XiaohongshuVideo,
)
from ..models import ManagedFacebookPage, OwnedVideoContent


# ═══════════════════════════════════════════════════════════
#  0. ALL VIDEOS (tất cả nền tảng gộp lại)
# ═══════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def all_external_videos(request):
    """Gom tất cả video từ Facebook Reels, TikTok Profile Videos, Instagram Reels.

    GET /api/scraper/all-videos/?page=1&page_size=24&q=keyword&sort=date&platform=facebook&min_plays=1000&date_from=2026-01-01&date_to=2026-06-30
    """
    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 24)))
    q = params.get('q', '').strip()
    sort = params.get('sort', 'date')
    platform = params.get('platform', '').strip()
    min_plays = _parse_int(params.get('min_plays'))
    date_from = params.get('date_from', '').strip()
    date_to = params.get('date_to', '').strip()

    items = []

    # Facebook Reels
    if not platform or platform == 'facebook':
        fb_qs = FacebookReel.objects.select_related('fanpage').all()
        if min_plays is not None:
            fb_qs = fb_qs.filter(views_count__gte=min_plays)
        if date_from:
            fb_qs = fb_qs.filter(date_posted__date__gte=date_from)
        if date_to:
            fb_qs = fb_qs.filter(date_posted__date__lte=date_to)
        for r in fb_qs:
            if q and not _unaccent_match(r.content, q) and not any(q.lower().strip('#') in h.lower() for h in (r.hashtags or [])):
                continue
            items.append({
                'platform': 'facebook',
                'post_id': r.post_id,
                'url': r.url,
                'description': r.content,
                'thumbnail_url': r.thumbnail_url,
                'duration_seconds': r.duration_seconds,
                'play_count': r.views_count,
                'likes_count': r.likes_count,
                'comments_count': r.comments_count,
                'date_posted': r.date_posted,
                'author_id': r.fanpage.profile_id if r.fanpage else '',
                'author_name': r.fanpage.name if r.fanpage else '',
                'author_avatar': r.fanpage.avatar_url if r.fanpage else '',
                'author_username': r.fanpage.handle if r.fanpage else '',
            })

    # TikTok Profile Videos
    if not platform or platform == 'tiktok':
        tt_qs = TikTokProfileVideo.objects.select_related('profile').all()
        if min_plays is not None:
            tt_qs = tt_qs.filter(play_count__gte=min_plays)
        if date_from:
            tt_qs = tt_qs.filter(date_posted__date__gte=date_from)
        if date_to:
            tt_qs = tt_qs.filter(date_posted__date__lte=date_to)
        for v in tt_qs:
            if q and not _unaccent_match(v.description, q) and not any(q.lower().strip('#') in h.lower() for h in (v.hashtags or [])):
                continue
            items.append({
                'platform': 'tiktok',
                'post_id': v.video_id,
                'url': v.url,
                'description': v.description,
                'thumbnail_url': v.cover_image,
                'duration_seconds': v.video_duration,
                'play_count': v.play_count,
                'likes_count': v.digg_count,
                'comments_count': v.comment_count,
                'date_posted': v.date_posted,
                'author_id': '',
                'author_name': v.profile.nickname if v.profile else '',
                'author_avatar': v.profile.avatar_url if v.profile else '',
                'author_username': v.profile.username if v.profile else '',
            })

    # Instagram Reels
    if not platform or platform == 'instagram':
        ig_qs = InstagramReel.objects.select_related('profile').all()
        if min_plays is not None:
            ig_qs = ig_qs.filter(play_count__gte=min_plays)
        if date_from:
            ig_qs = ig_qs.filter(date_posted__date__gte=date_from)
        if date_to:
            ig_qs = ig_qs.filter(date_posted__date__lte=date_to)
        for r in ig_qs:
            if q and not _unaccent_match(r.description, q) and not any(q.lower().strip('#') in h.lower() for h in (r.hashtags or [])):
                continue
            items.append({
                'platform': 'instagram',
                'post_id': r.post_id,
                'url': r.url,
                'description': r.description,
                'thumbnail_url': r.thumbnail_drive_url or r.thumbnail_url or '',
                'duration_seconds': r.duration_seconds,
                'play_count': r.play_count,
                'likes_count': r.likes_count,
                'comments_count': r.comments_count,
                'date_posted': r.date_posted,
                'author_id': '',
                'author_name': r.profile.username if r.profile else '',
                'author_avatar': r.profile.avatar_url if r.profile else '',
                'author_username': r.profile.username if r.profile else '',
            })

    # Sort
    if sort == 'plays':
        items.sort(key=lambda x: x['play_count'], reverse=True)
    elif sort == 'likes':
        items.sort(key=lambda x: x['likes_count'], reverse=True)
    else:
        items.sort(key=lambda x: x['date_posted'], reverse=True)

    total = len(items)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = items[start:start + page_size]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'videos': paginated,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def owned_channel_videos(request):
    """Tất cả video từ các kênh nội bộ (is_owned=True) trên mọi nền tảng.

    GET /api/scraper/owned/videos/?page=1&page_size=24&platform=tiktok&sort=date&q=keyword
    """
    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 24)))
    q = params.get('q', '').strip()
    sort = params.get('sort', 'date')
    platform = params.get('platform', '').strip()

    items = []

    # TikTok — TikTokProfileVideo WHERE profile.is_owned=True
    if not platform or platform == 'tiktok':
        tt_qs = TikTokProfileVideo.objects.select_related('profile').filter(profile__is_owned=True)
        for v in tt_qs:
            if q and not _unaccent_match(v.description, q):
                continue
            items.append({
                'platform': 'tiktok',
                'post_id': v.video_id,
                'url': v.url,
                'description': v.description,
                'thumbnail_url': v.cover_image or '',
                'duration_seconds': v.video_duration,
                'play_count': v.play_count,
                'likes_count': v.digg_count,
                'comments_count': v.comment_count,
                'date_posted': v.date_posted,
                'author_name': v.profile.nickname if v.profile else '',
                'author_avatar': v.profile.avatar_url if v.profile else '',
                'author_username': v.profile.username if v.profile else '',
            })

    # Instagram — InstagramReel WHERE profile.is_owned=True
    if not platform or platform == 'instagram':
        ig_qs = InstagramReel.objects.select_related('profile').filter(profile__is_owned=True)
        for r in ig_qs:
            if q and not _unaccent_match(r.description, q):
                continue
            items.append({
                'platform': 'instagram',
                'post_id': r.post_id,
                'url': r.url,
                'description': r.description,
                'thumbnail_url': r.thumbnail_drive_url or r.thumbnail_url or '',
                'duration_seconds': r.duration_seconds,
                'play_count': r.play_count,
                'likes_count': r.likes_count,
                'comments_count': r.comments_count,
                'date_posted': r.date_posted,
                'author_name': r.profile.username if r.profile else '',
                'author_avatar': r.profile.avatar_url if r.profile else '',
                'author_username': r.profile.username if r.profile else '',
            })

    # Douyin — DouyinVideo WHERE search_keyword='@{owned_username}'
    if not platform or platform == 'douyin':
        owned_usernames = list(
            DouyinProfile.objects.filter(is_owned=True, username__gt='').values_list('username', flat=True)
        )
        if owned_usernames:
            owned_keywords = [f'@{u}' for u in owned_usernames]
            dy_qs = DouyinVideo.objects.filter(search_keyword__in=owned_keywords)
            for v in dy_qs:
                if q and not _unaccent_match(v.description, q):
                    continue
                items.append({
                    'platform': 'douyin',
                    'post_id': v.post_id,
                    'url': v.url,
                    'description': v.description,
                    'thumbnail_url': v.preview_image or '',
                    'duration_seconds': v.video_duration,
                    'play_count': 0,
                    'likes_count': v.digg_count,
                    'comments_count': v.comment_count,
                    'date_posted': v.date_posted,
                    'author_name': v.author_display_name,
                    'author_avatar': v.author_avatar or '',
                    'author_username': v.author_username,
                })

    # Xiaohongshu — XiaohongshuVideo WHERE profile.is_owned=True
    if not platform or platform == 'xiaohongshu':
        xhs_qs = XiaohongshuVideo.objects.select_related('profile').filter(profile__is_owned=True)
        for v in xhs_qs:
            if q and not _unaccent_match((v.title or '') + ' ' + (v.description or ''), q):
                continue
            items.append({
                'platform': 'xiaohongshu',
                'post_id': v.note_id,
                'url': v.url,
                'description': v.title or v.description,
                'thumbnail_url': v.thumbnail_drive_url or v.thumbnail_url or '',
                'duration_seconds': v.duration_seconds,
                'play_count': 0,
                'likes_count': v.liked_count,
                'comments_count': v.comments_count,
                'date_posted': v.date_posted,
                'author_name': v.author_name,
                'author_avatar': v.author_avatar or '',
                'author_username': v.author_id,
            })

    # Facebook — OwnedVideoContent (kênh Facebook nội bộ)
    if not platform or platform == 'facebook':
        fb_qs = OwnedVideoContent.objects.select_related('managed_page').all()
        for v in fb_qs:
            if q and not _unaccent_match(v.caption, q):
                continue
            page = v.managed_page
            items.append({
                'platform': 'facebook',
                'post_id': v.post_id,
                'url': v.permalink_url or '',
                'description': v.caption or '',
                'thumbnail_url': v.thumbnail_url or '',
                'duration_seconds': None,
                'play_count': v.view_count,
                'likes_count': v.like_count,
                'comments_count': v.comment_count,
                'date_posted': v.published_at,
                'author_name': page.name if page else '',
                'author_avatar': page.avatar_url if page else '',
                'author_username': page.page_id if page else '',
            })

    # Sort
    reverse = True
    if sort == 'plays':
        items.sort(key=lambda x: x.get('play_count') or 0, reverse=reverse)
    elif sort == 'likes':
        items.sort(key=lambda x: x.get('likes_count') or 0, reverse=reverse)
    else:
        items.sort(key=lambda x: str(x.get('date_posted') or ''), reverse=reverse)

    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page_num - 1) * page_size

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'videos': items[start:start + page_size],
    })


def _unaccent_match(text: str, query: str) -> bool:
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


# ═══════════════════════════════════════════════════════════
#  1. KEYWORD AUTOCOMPLETE
# ═══════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def keyword_suggest(request):
    """Top 10 keywords khớp query (unaccent). 'da quy' khớp 'đá quý'.

    GET /api/scraper/keywords/suggest/?q=da+quy
    """
    q = request.query_params.get('q', '').strip()
    if not q:
        top = SearchKeyword.objects.all().order_by('-hit_count')[:10]
    else:
        q_ascii = unidecode(q).lower()
        top = SearchKeyword.objects.filter(
            cleaned_keyword_ascii__icontains=q_ascii
        ).order_by('-hit_count')[:10]

    results = [{'id': kw.id, 'keyword': kw.cleaned_keyword, 'hits': kw.hit_count} for kw in top]
    return Response({'suggestions': results})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def keyword_hit(request):
    """Tăng hit_count khi user chọn keyword. Tạo mới nếu chưa tồn tại.

    POST /api/scraper/keywords/hit/
    Body: { "keyword": "đá quý" }
    """
    keyword_text = (request.data.get('keyword', '') or '').strip()
    if not keyword_text:
        return Response({'error': 'keyword is required'}, status=400)

    ascii_version = unidecode(keyword_text).lower()
    kw, created = SearchKeyword.objects.get_or_create(
        cleaned_keyword=keyword_text,
        defaults={
            'cleaned_keyword_ascii': ascii_version,
            'hit_count': 1,
            'is_google_active': False,
        }
    )
    if not created:
        kw.hit_count += 1
        kw.save(update_fields=['hit_count', 'updated_at'])

    return Response({'id': kw.id, 'keyword': kw.cleaned_keyword, 'hits': kw.hit_count, 'created': created})


# ═══════════════════════════════════════════════════════════
#  2. SEARCH REELS IN DB (Tìm nhanh — không gọi API ngoài)
# ═══════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def search_reels(request):
    """Tìm reels đã có trong DB theo keyword (caption + hashtags, unaccent).

    Đây là bước ĐẦU TIÊN khi user gõ keyword — trả về ngay lập tức.
    Chỉ khi user bấm "Tìm mới" mới gọi BrightData.

    GET /api/scraper/reels/search/?q=da+quy&page=1&page_size=20&min_views=1000

    Filters:
    - q: search caption + hashtags (unaccent)
    - min_views, min_likes, date_from, date_to
    - fanpage_id: lọc theo 1 fanpage cụ thể
    - page, page_size: pagination
    """
    params = request.query_params
    q = params.get('q', '').strip()
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 20)))
    min_views = _parse_int(params.get('min_views'))
    min_likes = _parse_int(params.get('min_likes'))
    fanpage_id = _parse_int(params.get('fanpage_id'))
    date_from = params.get('date_from', '').strip()
    date_to = params.get('date_to', '').strip()

    qs = FacebookReel.objects.select_related('fanpage').all()

    # SQL filters
    if fanpage_id:
        qs = qs.filter(fanpage_id=fanpage_id)
    if min_views is not None:
        qs = qs.filter(views_count__gte=min_views)
    if min_likes is not None:
        qs = qs.filter(likes_count__gte=min_likes)
    if date_from:
        qs = qs.filter(date_posted__date__gte=date_from)
    if date_to:
        qs = qs.filter(date_posted__date__lte=date_to)

    # Sort: mặc định mới nhất, hoặc theo sort param
    sort_by = params.get('sort', 'date')
    if sort_by == 'views':
        qs = qs.order_by('-views_count')
    elif sort_by == 'likes':
        qs = qs.order_by('-likes_count')
    else:
        qs = qs.order_by('-date_posted')

    # Keyword search: hashtags (SQL exact) + caption (unaccent Python)
    if q:
        q_lower = q.lower().strip('#')

        # Phase 1: thử match hashtag trước (nhanh, SQL)
        hashtag_matches = qs.filter(hashtags__icontains=q_lower)
        hashtag_ids = set(hashtag_matches.values_list('id', flat=True))

        # Phase 2: match caption (unaccent, Python-side)
        caption_matches = [r for r in qs if _unaccent_match(r.content, q)]
        caption_ids = {r.id for r in caption_matches}

        # Gộp kết quả (union unique IDs)
        all_match_ids = hashtag_ids | caption_ids
        if all_match_ids:
            filtered_reels = list(qs.filter(id__in=all_match_ids).order_by('-views_count'))
        else:
            filtered_reels = []
    else:
        filtered_reels = list(qs)

    # Pagination
    total = len(filtered_reels)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = filtered_reels[start:start + page_size]

    results = [
        {
            'post_id': r.post_id,
            'shortcode': r.shortcode,
            'url': r.url,
            'content': r.content,
            'hashtags': r.hashtags,
            'video_url': r.video_url,
            'thumbnail_url': r.thumbnail_url,
            'duration_seconds': r.duration_seconds,
            'has_audio': r.has_audio,
            'date_posted': r.date_posted,
            'views_count': r.views_count,
            'likes_count': r.likes_count,
            'comments_count': r.comments_count,
            'shares_count': r.shares_count,
            'fanpage': {
                'id': r.fanpage.id,
                'name': r.fanpage.name,
                'handle': r.fanpage.handle,
                'avatar_url': r.fanpage.avatar_url,
            } if r.fanpage else None,
        }
        for r in paginated
    ]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'reels': results,
    })


# ═══════════════════════════════════════════════════════════
#  3. KEYWORD CRUD
# ═══════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def keyword_list(request):
    """Danh sách tất cả keywords."""
    keywords = SearchKeyword.objects.all().order_by('-hit_count')
    results = [
        {
            'id': kw.id,
            'keyword': kw.cleaned_keyword,
            'hits': kw.hit_count,
            'is_google_active': kw.is_google_active,
            'last_searched_at': kw.last_searched_at,
        }
        for kw in keywords
    ]
    return Response({'keywords': results, 'count': len(results)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def keyword_create(request):
    """Tạo keyword mới.

    POST /api/scraper/keywords/create/
    Body: { "keyword": "trang sức bạc", "is_google_active": true }
    """
    keyword_text = (request.data.get('keyword', '') or '').strip()
    if not keyword_text:
        return Response({'error': 'keyword is required'}, status=400)

    ascii_version = unidecode(keyword_text).lower()
    is_active = request.data.get('is_google_active', True)

    kw, created = SearchKeyword.objects.get_or_create(
        cleaned_keyword=keyword_text,
        defaults={
            'cleaned_keyword_ascii': ascii_version,
            'hit_count': 0,
            'is_google_active': is_active,
        }
    )
    if not created:
        return Response({'error': 'Keyword already exists', 'id': kw.id}, status=409)

    return Response({
        'id': kw.id, 'keyword': kw.cleaned_keyword,
        'is_google_active': kw.is_google_active, 'created': True,
    }, status=201)


# ═══════════════════════════════════════════════════════════
#  4. FANPAGE DISCOVERY (gọi BrightData — "Tìm mới")
# ═══════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def trigger_discovery(request):
    """Trigger Google SERP discovery — CHỈ khi user bấm "Tìm mới".

    POST /api/scraper/discover/
    Body: { "keyword_id": 5 }           → discover 1 keyword
    Body: { "keyword": "đá quý" }       → tạo keyword nếu chưa có, rồi discover
    Body: { "all_active": true }         → discover all active keywords
    """
    from ..tasks import discover_pages_from_google_task, discover_all_active_keywords_task

    all_active = request.data.get('all_active', False)
    if all_active:
        discover_all_active_keywords_task.delay()
        count = SearchKeyword.objects.filter(is_google_active=True).count()
        return Response({'status': 'ok', 'message': f'Đã dispatch {count} keyword(s).'})

    # Tìm hoặc tạo keyword
    keyword_id = request.data.get('keyword_id')
    keyword_text = (request.data.get('keyword', '') or '').strip()

    if keyword_id:
        try:
            kw = SearchKeyword.objects.get(id=keyword_id)
        except SearchKeyword.DoesNotExist:
            return Response({'error': f'Keyword {keyword_id} not found'}, status=404)
    elif keyword_text:
        ascii_version = unidecode(keyword_text).lower()
        kw, _ = SearchKeyword.objects.get_or_create(
            cleaned_keyword=keyword_text,
            defaults={
                'cleaned_keyword_ascii': ascii_version,
                'hit_count': 1,
                'is_google_active': True,
            }
        )
    else:
        return Response({'error': 'keyword_id or keyword is required'}, status=400)

    discover_pages_from_google_task.delay(kw.id)
    return Response({
        'status': 'ok',
        'keyword_id': kw.id,
        'message': f'Đang tìm kiếm fanpages cho "{kw.cleaned_keyword}" trên Google...',
    })


# ═══════════════════════════════════════════════════════════
#  5. FANPAGES LIST (paginated) + DETAIL + TOGGLES
# ═══════════════════════════════════════════════════════════

def _serialize_fanpage(p):
    return {
        'id': p.id,
        'profile_id': p.profile_id,
        'name': p.name,
        'handle': p.handle,
        'page_url': p.page_url,
        'avatar_url': p.avatar_url,
        'is_verified': p.is_verified,
        'followers_count': p.followers_count,
        'likes_count': p.likes_count,
        'is_visible_on_ui': p.is_visible_on_ui,
        'is_periodic_crawl': p.is_periodic_crawl,
        'is_bookmarked': p.is_bookmarked,
        'is_initial_scraped': p.is_initial_scraped,
        'scraping_status': p.scraping_status,
        'last_scraped_at': p.last_scraped_at,
        'scrape_error': p.scrape_error,
        'reels_count': p.reels.count(),
        'created_at': p.created_at,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def discovered_fanpages(request):
    """Danh sách fanpages với pagination + search + filter.

    GET /api/scraper/fanpages/?page=1&page_size=12&search=jewelry&bookmarked=true
    """
    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(50, max(1, _parse_int(params.get('page_size'), 12)))
    search = params.get('search', '').strip()
    bookmarked = params.get('bookmarked', '').strip()
    periodic = params.get('periodic', '').strip()

    qs = ScrapedFanpage.objects.filter(is_visible_on_ui=True)

    if bookmarked == 'true':
        qs = qs.filter(is_bookmarked=True)
    if periodic == 'true':
        qs = qs.filter(is_periodic_crawl=True)

    # Sort: bookmarked first, then by followers
    qs = qs.order_by('-is_bookmarked', '-followers_count')

    # Unaccent search on name
    if search:
        all_pages = list(qs)
        all_pages = [p for p in all_pages if _unaccent_match(p.name, search)]
    else:
        all_pages = list(qs)

    total = len(all_pages)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = all_pages[start:start + page_size]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'fanpages': [_serialize_fanpage(p) for p in paginated],
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def fanpage_detail(request, fanpage_id: int):
    """Chi tiết 1 fanpage + tổng hợp metrics."""
    from django.db.models import Sum
    try:
        fp = ScrapedFanpage.objects.get(id=fanpage_id)
    except ScrapedFanpage.DoesNotExist:
        return Response({'error': 'Not found'}, status=404)

    totals = fp.reels.aggregate(
        total_views=Sum('views_count'),
        total_likes=Sum('likes_count'),
        total_comments=Sum('comments_count'),
        total_shares=Sum('shares_count'),
    )

    data = _serialize_fanpage(fp)
    data['total_views'] = totals['total_views'] or 0
    data['total_likes'] = totals['total_likes'] or 0
    data['total_comments'] = totals['total_comments'] or 0
    data['total_shares'] = totals['total_shares'] or 0

    return Response(data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fanpage_toggle(request, fanpage_id: int):
    """Toggle flags trên fanpage.

    POST /api/scraper/fanpages/<id>/toggle/
    Body: { "field": "is_bookmarked" }  hoặc "is_periodic_crawl"
    """
    field = request.data.get('field', '')
    if field not in ('is_bookmarked', 'is_periodic_crawl'):
        return Response({'error': 'field must be is_bookmarked or is_periodic_crawl'}, status=400)

    try:
        fp = ScrapedFanpage.objects.get(id=fanpage_id)
    except ScrapedFanpage.DoesNotExist:
        return Response({'error': 'Not found'}, status=404)

    current = getattr(fp, field)
    setattr(fp, field, not current)
    fp.save(update_fields=[field, 'updated_at'])

    return Response({'id': fp.id, field: getattr(fp, field)})


# ═══════════════════════════════════════════════════════════
#  6. TRIGGER SCRAPE REELS FOR A PAGE
# ═══════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def trigger_scrape_reels(request):
    """Trigger cào reels: 300 lần đầu (is_initial_scraped=False), 10 từ lần sau.

    POST /api/scraper/fanpages/scrape-reels/
    Body: { "fanpage_id": 5 }
    """
    fanpage_id = request.data.get('fanpage_id')

    if not fanpage_id:
        return Response({'error': 'fanpage_id is required'}, status=400)

    try:
        fp = ScrapedFanpage.objects.get(id=fanpage_id)
    except ScrapedFanpage.DoesNotExist:
        return Response({'error': f'Fanpage {fanpage_id} not found'}, status=404)

    if fp.scraping_status == 'processing':
        return Response({'status': 'ok', 'message': f'{fp.name} đang được cào.', 'is_scraping': True})

    # 300 lần đầu, 10 từ lần sau
    num = 300 if not fp.is_initial_scraped else 10

    from ..tasks import scrape_reels_for_page_task
    scrape_reels_for_page_task.delay(fp.id, num_of_posts=num)

    return Response({
        'status': 'ok',
        'message': f'Đã gửi yêu cầu cào {num} reels cho {fp.name}.',
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fanpage_scrape_by_url(request):
    """Nhập Facebook page URL → tạo fanpage placeholder + trigger cào reels.

    POST /api/scraper/fanpages/scrape-by-url/
    Body: { "url": "https://www.facebook.com/katjewelry" }
    """
    from django.db import transaction

    page_url = (request.data.get('url', '') or '').strip()
    if not page_url:
        return Response({'error': 'url is required'}, status=400)

    if 'facebook.com' not in page_url:
        return Response({'error': 'URL không hợp lệ. Ví dụ: https://www.facebook.com/pagename'}, status=400)

    from ..services.brightdata_serp import clean_facebook_url, extract_handle_from_url

    clean_url = clean_facebook_url(page_url)
    handle = extract_handle_from_url(clean_url)

    if not handle and 'profile.php' not in clean_url:
        return Response({'error': 'Không thể trích xuất tên page từ URL.'}, status=400)

    with transaction.atomic():
        # Tìm theo handle hoặc page_url
        fp = None
        if handle:
            fp = ScrapedFanpage.objects.select_for_update().filter(handle=handle).first()
        if not fp:
            fp = ScrapedFanpage.objects.select_for_update().filter(page_url=clean_url).first()

        if fp:
            if fp.scraping_status == 'processing':
                return Response({
                    'status': 'ok',
                    'message': f'{fp.name or handle} đang được cào.',
                    'is_scraping': True,
                    'fanpage_id': fp.id,
                })

            if fp.is_initial_scraped:
                return Response({
                    'status': 'ok',
                    'message': f'{fp.name or handle} đã có trong hệ thống.',
                    'already_exists': True,
                    'fanpage_id': fp.id,
                })
        else:
            # Dùng temp profile_id dựa trên handle để tránh unique conflict
            # (profile_id thật sẽ được ghi đè khi task cào xong)
            temp_id = f'tmp_{handle}' if handle else f'tmp_{abs(hash(clean_url)) % 10**12}'
            fp, _ = ScrapedFanpage.objects.get_or_create(
                profile_id=temp_id,
                defaults={
                    'name': handle or 'Facebook Page',
                    'handle': handle,
                    'page_url': clean_url,
                    'is_visible_on_ui': True,
                },
            )

        fp.scraping_status = 'processing'
        fp.scrape_error = None
        fp.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])

    num = 300 if not fp.is_initial_scraped else 10

    from ..tasks import scrape_reels_for_page_task
    scrape_reels_for_page_task.delay(fp.id, num_of_posts=num)

    return Response({
        'status': 'ok',
        'message': f'Đã gửi yêu cầu cào reels cho {fp.name or handle}.',
        'fanpage_id': fp.id,
    })


# ═══════════════════════════════════════════════════════════
#  7. TIKTOK — Search + List videos
# ═══════════════════════════════════════════════════════════


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def tiktok_search(request):
    """TikTok keyword search — gọi TikHub synchronously, trả CDN URL về ngay.

    Drive upload chạy async bên trong ingest_tikhub_videos (upload_thumbnail_to_drive_task.delay).
    Lần sau user query lại, preview_image đã là Drive URL nếu upload xong.

    POST /api/scraper/tiktok/search/
    Body: { "keyword": "jewelry", "num_of_posts": 30, "country": "VN" }
    """
    keyword = (request.data.get('keyword', '') or '').strip()
    if not keyword:
        return Response({'error': 'keyword is required'}, status=400)

    num = min(200, max(1, int(request.data.get('num_of_posts', 30) or 30)))
    country = request.data.get('country', 'VN')

    try:
        from ..services.tikhub_tiktok import search_tiktok_by_keyword, ingest_tikhub_videos
        videos = search_tiktok_by_keyword(keyword=keyword, count=num, region=country)
        if not videos:
            return Response({
                'status': 'ok',
                'message': f'Không tìm thấy video nào cho "{keyword}".',
                'created': 0, 'updated': 0,
            })
        # Lưu CDN URL vào DB ngay; Drive upload chạy async bên trong
        result = ingest_tikhub_videos(videos, search_keyword=keyword)
        return Response({
            'status': 'ok',
            'message': f'Đã tìm thấy {result["created"]} video mới cho "{keyword}".',
            'created': result['created'],
            'updated': result['updated'],
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f'[TIKTOK-SEARCH] error: {e}', exc_info=True)
        return Response({'error': f'Lỗi tìm kiếm: {str(e)[:200]}'}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tiktok_videos(request):
    """Danh sách TikTok videos với pagination + search + filter.

    GET /api/scraper/tiktok/videos/?q=jewelry&page=1&page_size=24&min_plays=1000&sort=date
    """
    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 24)))
    q = params.get('q', '').strip()
    min_plays = _parse_int(params.get('min_plays'))
    date_from = params.get('date_from', '').strip()
    date_to = params.get('date_to', '').strip()
    sort = params.get('sort', 'date')
    qs = TikTokVideo.objects.all()

    if min_plays is not None:
        qs = qs.filter(play_count__gte=min_plays)
    if date_from:
        qs = qs.filter(date_posted__date__gte=date_from)
    if date_to:
        qs = qs.filter(date_posted__date__lte=date_to)

    if sort == 'plays':
        qs = qs.order_by('-play_count')
    elif sort == 'likes':
        qs = qs.order_by('-digg_count')
    else:
        qs = qs.order_by('-date_posted')

    # Unaccent search on description
    # Filter by search_keyword param (exact match cho keyword đã cào)
    kw_filter = params.get('search_keyword', '').strip()
    if kw_filter:
        qs = qs.filter(search_keyword__icontains=kw_filter)

    if q:
        filtered = [v for v in qs if
                    _unaccent_match(v.description, q) or
                    _unaccent_match(v.search_keyword, q) or
                    any(q.lower().strip('#') in h.lower() for h in (v.hashtags or []))]
    else:
        filtered = list(qs)

    total = len(filtered)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = filtered[start:start + page_size]

    results = [
        {
            'post_id': v.post_id,
            'shortcode': v.shortcode,
            'url': v.url,
            'description': v.description,
            'hashtags': v.hashtags,
            'video_url': v.video_url,
            'cdn_url': v.cdn_url,
            'preview_image': v.preview_image,
            'video_duration': v.video_duration,
            'region': v.region,
            'play_count': v.play_count,
            'digg_count': v.digg_count,
            'comment_count': v.comment_count,
            'share_count': v.share_count,
            'collect_count': v.collect_count,
            'music_title': v.music_title,
            'search_keyword': v.search_keyword,
            'date_posted': v.date_posted,
            'author': {
                'id': v.author_id,
                'username': v.author_username,
                'display_name': v.author_display_name,
                'avatar_url': v.author_avatar,
                'url': v.author_url,
                'followers': v.author_followers,
                'is_verified': v.author_is_verified,
            },
        }
        for v in paginated
    ]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'videos': results,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tiktok_keyword_suggest(request):
    """Autocomplete: keywords đã cào trên TikTok, sắp xếp theo số video.

    GET /api/scraper/tiktok/keywords/suggest/?q=da
    """
    from django.db.models import Count

    q = request.query_params.get('q', '').strip()

    agg = (
        TikTokVideo.objects
        .exclude(search_keyword='')
        .values('search_keyword')
        .annotate(count=Count('id'))
        .order_by('-count')
    )

    if q:
        q_ascii = unidecode(q).lower()
        results = [
            {'keyword': row['search_keyword'], 'count': row['count']}
            for row in agg
            if q_ascii in unidecode(row['search_keyword']).lower()
        ][:10]
    else:
        results = [{'keyword': row['search_keyword'], 'count': row['count']} for row in agg[:10]]

    return Response({'suggestions': results})


# ═══════════════════════════════════════════════════════════
#  8. TIKTOK PROFILE — Scrape posts by profile URL
# ═══════════════════════════════════════════════════════════


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def tiktok_profile_scrape(request):
    """Nhập TikTok username → tạo profile + trigger cào posts.

    POST /api/scraper/tiktok/profiles/scrape/
    Body: { "username": "mixigaming" }
    Cũng chấp nhận URL để tương thích ngược: { "url": "https://www.tiktok.com/@mixigaming" }
    """
    import re

    raw = (request.data.get('username') or request.data.get('url') or '').strip()
    if not raw:
        return Response({'error': 'username is required'}, status=400)

    # Nếu là URL thì extract username, nếu là @username thì bỏ @
    url_match = re.search(r'tiktok\.com/@([A-Za-z0-9_.]+)', raw)
    if url_match:
        username = url_match.group(1)
    else:
        username = raw.lstrip('@').strip()

    if not username or not re.match(r'^[A-Za-z0-9_.]+$', username):
        return Response({'error': 'Username không hợp lệ'}, status=400)

    canonical_url = f'https://www.tiktok.com/@{username}'
    is_owned = bool(request.data.get('is_owned', False))

    from django.db import transaction

    with transaction.atomic():
        profile, created = TikTokProfile.objects.select_for_update().get_or_create(
            username=username,
            defaults={
                'profile_id': '',
                'url': canonical_url,
                'nickname': '',
                'is_owned': is_owned,
            },
        )

        if profile.scraping_status == 'processing':
            return Response({
                'status': 'ok',
                'message': f'@{username} đang được cào.',
                'is_scraping': True,
                'profile_id': profile.id,
            })

        if not created and profile.is_initial_scraped:
            if is_owned and not profile.is_owned:
                profile.is_owned = True
                profile.save(update_fields=['is_owned', 'updated_at'])
            return Response({
                'status': 'ok',
                'message': f'@{username} đã có trong hệ thống.',
                'already_exists': True,
                'profile_id': profile.id,
            })

        profile.scraping_status = 'processing'
        profile.scrape_error = None
        update_fields = ['scraping_status', 'scrape_error', 'updated_at']
        if is_owned and not profile.is_owned:
            profile.is_owned = True
            update_fields.append('is_owned')
        profile.save(update_fields=update_fields)

    # ── Fetch batch đầu từ TikHub SYNCHRONOUSLY (chỉ 1 page ~20 posts, rất nhanh) ──
    # Trả data về cho user ngay sau khi có. Drive upload chạy async bên trong
    # ingest_tikhub_profile_posts (via .delay()). Background task tiếp tục cào
    # phần còn lại sau khi response đã được trả về.
    try:
        from ..services.tikhub_tiktok_profile import fetch_user_posts, ingest_tikhub_profile_posts

        sec_uid = profile.sec_uid or ''
        items = fetch_user_posts(username, count=20, sec_uid=sec_uid)

        if not items:
            profile.scraping_status = 'idle'
            profile.scrape_error = 'TikHub không trả về posts (profile không có video hoặc username không tồn tại)'
            profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
            return Response({'error': 'Không tìm thấy posts cho username này'}, status=404)

        # Lưu vào DB — Drive upload thumbnail chạy async bên trong (upload_thumbnail_to_drive_task.delay)
        result = ingest_tikhub_profile_posts(items, profile)

        # Kick off background task để cào nốt 600 posts còn lại (non-blocking)
        from ..tasks import scrape_tiktok_profile_posts_task
        scrape_tiktok_profile_posts_task.delay(profile.id, num_of_posts=600)

        # Trả data về cho user ngay (status vẫn là 'processing' vì background task đang chạy)
        profile.refresh_from_db()
        return Response({
            'status': 'ok',
            'message': f'Đã cào {result["created"]} posts đầu cho @{username}. Đang tiếp tục cào thêm...',
            'profile_id': profile.id,
            'newly_scraped': True,
            'profile': {
                'id': profile.id,
                'username': profile.username,
                'nickname': profile.nickname or '',
                'avatar_url': profile.avatar_url or '',
                'biography': profile.biography or '',
                'is_verified': profile.is_verified,
                'followers_count': profile.followers_count,
                'following_count': profile.following_count,
                'likes_count': profile.likes_count,
                'videos_count': profile.videos_count,
                'scraping_status': profile.scraping_status,
            },
            'initial_posts_count': result['created'],
        })

    except Exception as e:
        profile.scraping_status = 'failed'
        profile.scrape_error = str(e)[:500]
        profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
        import logging
        logging.getLogger(__name__).error(f'[PROFILE-SCRAPE] @{username}: {e}', exc_info=True)
        return Response({'error': f'Lỗi khi cào: {str(e)[:200]}'}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tiktok_profiles_list(request):
    """Danh sách TikTok profiles với pagination + search.

    GET /api/scraper/tiktok/profiles/?page=1&page_size=12&search=hima
    """
    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 12)))
    search = params.get('search', '').strip()
    sort_by = params.get('sort_by', 'followers')  # 'followers' | 'recent'

    is_owned_param = params.get('is_owned', '').strip()

    # is_bookmarked luôn ưu tiên lên đầu, sau đó mới theo sort_by
    secondary_order = '-created_at' if sort_by == 'recent' else '-followers_count'
    qs = TikTokProfile.objects.all().order_by('-is_bookmarked', secondary_order)

    if is_owned_param == 'true':
        qs = qs.filter(is_owned=True)
    elif is_owned_param == 'false':
        qs = qs.filter(is_owned=False)

    if search:
        search_ascii = unidecode(search).lower()
        qs = [p for p in qs if
              search_ascii in unidecode(p.username).lower() or
              search_ascii in unidecode(p.nickname).lower()]
    else:
        qs = list(qs)

    total = len(qs)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = qs[start:start + page_size]

    results = [
        {
            'id': p.id,
            'profile_id': p.profile_id,
            'username': p.username,
            'nickname': p.nickname,
            'url': p.url,
            'avatar_url': p.avatar_url or '',
            'biography': p.biography or '',
            'is_verified': p.is_verified,
            'followers_count': p.followers_count,
            'following_count': p.following_count,
            'likes_count': p.likes_count,
            'videos_count': p.videos_count,
            'is_tracked': p.is_tracked,
            'is_bookmarked': p.is_bookmarked,
            'is_owned': p.is_owned,
            'is_initial_scraped': p.is_initial_scraped,
            'scraping_status': p.scraping_status,
            'scrape_error': p.scrape_error,
            'last_scraped_at': p.last_scraped_at,
            'created_at': p.created_at,
            'videos_in_db': TikTokProfileVideo.objects.filter(profile=p).count(),
        }
        for p in paginated
    ]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'profiles': results,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tiktok_profile_detail(request, profile_id):
    """Chi tiết 1 TikTok profile.

    GET /api/scraper/tiktok/profiles/<id>/
    """
    try:
        p = TikTokProfile.objects.get(id=profile_id)
    except TikTokProfile.DoesNotExist:
        return Response({'error': 'Profile not found'}, status=404)

    total_plays = 0
    total_diggs = 0
    total_comments = 0
    total_shares = 0
    videos = TikTokProfileVideo.objects.filter(profile=p)
    for v in videos:
        total_plays += v.play_count
        total_diggs += v.digg_count
        total_comments += v.comment_count
        total_shares += v.share_count

    return Response({
        'id': p.id,
        'profile_id': p.profile_id,
        'username': p.username,
        'nickname': p.nickname,
        'url': p.url,
        'avatar_url': p.avatar_url or '',
        'biography': p.biography or '',
        'is_verified': p.is_verified,
        'followers_count': p.followers_count,
        'following_count': p.following_count,
        'likes_count': p.likes_count,
        'videos_count': p.videos_count,
        'is_tracked': p.is_tracked,
        'is_bookmarked': p.is_bookmarked,
        'is_initial_scraped': p.is_initial_scraped,
        'scraping_status': p.scraping_status,
        'scrape_error': p.scrape_error,
        'last_scraped_at': p.last_scraped_at,
        'created_at': p.created_at,
        'videos_in_db': videos.count(),
        'total_plays': total_plays,
        'total_diggs': total_diggs,
        'total_comments': total_comments,
        'total_shares': total_shares,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tiktok_profile_videos(request, profile_id):
    """Danh sách videos của 1 TikTok profile với pagination + filter.

    GET /api/scraper/tiktok/profiles/<id>/videos/?page=1&page_size=24&sort=plays
    """
    try:
        profile = TikTokProfile.objects.get(id=profile_id)
    except TikTokProfile.DoesNotExist:
        return Response({'error': 'Profile not found'}, status=404)

    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 24)))
    sort = params.get('sort', 'date')
    q = params.get('q', '').strip()
    min_plays = _parse_int(params.get('min_plays'))

    qs = TikTokProfileVideo.objects.filter(profile=profile)

    if min_plays is not None:
        qs = qs.filter(play_count__gte=min_plays)

    if sort == 'plays':
        qs = qs.order_by('-play_count')
    elif sort == 'likes':
        qs = qs.order_by('-digg_count')
    else:
        qs = qs.order_by('-date_posted')

    if q:
        filtered = [v for v in qs if
                    _unaccent_match(v.description, q) or
                    any(q.lower().strip('#') in h.lower() for h in (v.hashtags or []))]
    else:
        filtered = list(qs)

    total = len(filtered)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = filtered[start:start + page_size]

    results = [
        {
            'video_id': v.video_id,
            'shortcode': v.shortcode,
            'url': v.url,
            'description': v.description,
            'hashtags': v.hashtags,
            'cover_image': v.cover_image or '',
            'video_duration': v.video_duration,
            'region': v.region,
            'post_type': v.post_type,
            'play_count': v.play_count,
            'digg_count': v.digg_count,
            'comment_count': v.comment_count,
            'share_count': v.share_count,
            'favorites_count': v.favorites_count,
            'music_title': v.music_title,
            'music_author': v.music_author,
            'date_posted': v.date_posted,
            'profile': {
                'id': profile.id,
                'username': profile.username,
                'nickname': profile.nickname,
                'avatar_url': profile.avatar_url or '',
            },
        }
        for v in paginated
    ]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'videos': results,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def tiktok_profile_toggle(request, profile_id):
    """Toggle bookmark / tracked cho 1 TikTok profile.

    POST /api/scraper/tiktok/profiles/<id>/toggle/
    Body: { "field": "is_bookmarked" | "is_tracked" }
    """
    try:
        profile = TikTokProfile.objects.get(id=profile_id)
    except TikTokProfile.DoesNotExist:
        return Response({'error': 'Profile not found'}, status=404)

    field = request.data.get('field', '')
    if field not in ('is_bookmarked', 'is_tracked'):
        return Response({'error': 'field must be is_bookmarked or is_tracked'}, status=400)

    current = getattr(profile, field)
    setattr(profile, field, not current)
    profile.save(update_fields=[field, 'updated_at'])

    return Response({
        'status': 'ok',
        field: not current,
    })


# ═══════════════════════════════════════════════════════════
#  9. INSTAGRAM PROFILE — Scrape reels by profile URL
# ═══════════════════════════════════════════════════════════


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def instagram_profile_scrape(request):
    """Nhập Instagram username → tạo profile + trigger cào reels qua TikHub.

    POST /api/scraper/instagram/profiles/scrape/
    Body: { "username": "lanh_1403_" }
    """
    import re
    from django.db import transaction

    username = (request.data.get('username', '') or '').strip().lstrip('@')
    if not username:
        return Response({'error': 'username is required'}, status=400)

    if not re.match(r'^[A-Za-z0-9_.]{1,30}$', username):
        return Response({'error': 'Username không hợp lệ (chỉ chứa chữ, số, dấu _ và .)'}, status=400)

    canonical_url = f'https://www.instagram.com/{username}/'
    is_owned = bool(request.data.get('is_owned', False))

    with transaction.atomic():
        profile, created = InstagramProfile.objects.select_for_update().get_or_create(
            username=username,
            defaults={
                'url': canonical_url,
                'is_owned': is_owned,
            },
        )

        if profile.scraping_status == 'processing':
            return Response({
                'status': 'ok',
                'message': f'@{username} đang được cào.',
                'is_scraping': True,
                'profile_id': profile.id,
            })

        if not created and profile.is_initial_scraped:
            if is_owned and not profile.is_owned:
                profile.is_owned = True
                profile.save(update_fields=['is_owned', 'updated_at'])
            return Response({
                'status': 'ok',
                'message': f'@{username} đã có trong hệ thống.',
                'already_exists': True,
                'profile_id': profile.id,
            })

        profile.scraping_status = 'processing'
        profile.scrape_error = None
        update_fields = ['scraping_status', 'scrape_error', 'updated_at']
        if is_owned and not profile.is_owned:
            profile.is_owned = True
            update_fields.append('is_owned')
        profile.save(update_fields=update_fields)

    num_of_posts = 600 if not profile.is_initial_scraped else 0

    from ..tasks import scrape_instagram_profile_reels_task
    scrape_instagram_profile_reels_task.delay(profile.id, num_of_posts=num_of_posts)

    return Response({
        'status': 'ok',
        'message': f'Đã gửi yêu cầu cào reels cho @{username}.',
        'profile_id': profile.id,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def instagram_profiles_list(request):
    """Danh sách Instagram profiles với pagination + search.

    GET /api/scraper/instagram/profiles/?page=1&page_size=12&search=djmie
    """
    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 12)))
    search = params.get('search', '').strip()

    is_owned_param = params.get('is_owned', '').strip()
    qs = InstagramProfile.objects.all()

    if is_owned_param == 'true':
        qs = qs.filter(is_owned=True)
    elif is_owned_param == 'false':
        qs = qs.filter(is_owned=False)

    if search:
        search_ascii = unidecode(search).lower()
        qs = [p for p in qs if search_ascii in unidecode(p.username).lower()]
    else:
        qs = list(qs)

    total = len(qs)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = qs[start:start + page_size]

    results = [
        {
            'id': p.id,
            'username': p.username,
            'url': p.url,
            'avatar_url': p.avatar_url or '',
            'is_verified': p.is_verified,
            'followers_count': p.followers_count,
            'following_count': p.following_count,
            'posts_count': p.posts_count,
            'is_tracked': p.is_tracked,
            'is_bookmarked': p.is_bookmarked,
            'is_owned': p.is_owned,
            'is_initial_scraped': p.is_initial_scraped,
            'scraping_status': p.scraping_status,
            'scrape_error': p.scrape_error,
            'last_scraped_at': p.last_scraped_at,
            'created_at': p.created_at,
            'reels_in_db': InstagramReel.objects.filter(profile=p).count(),
        }
        for p in paginated
    ]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'profiles': results,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def instagram_profile_detail(request, profile_id):
    """Chi tiết 1 Instagram profile.

    GET /api/scraper/instagram/profiles/<id>/
    """
    try:
        p = InstagramProfile.objects.get(id=profile_id)
    except InstagramProfile.DoesNotExist:
        return Response({'error': 'Profile not found'}, status=404)

    total_plays = 0
    total_likes = 0
    total_comments = 0
    reels = InstagramReel.objects.filter(profile=p)
    for r in reels:
        total_plays += r.play_count
        total_likes += r.likes_count
        total_comments += r.comments_count

    return Response({
        'id': p.id,
        'username': p.username,
        'url': p.url,
        'avatar_url': p.avatar_url or '',
        'is_verified': p.is_verified,
        'followers_count': p.followers_count,
        'following_count': p.following_count,
        'posts_count': p.posts_count,
        'is_tracked': p.is_tracked,
        'is_bookmarked': p.is_bookmarked,
        'is_initial_scraped': p.is_initial_scraped,
        'scraping_status': p.scraping_status,
        'scrape_error': p.scrape_error,
        'last_scraped_at': p.last_scraped_at,
        'created_at': p.created_at,
        'reels_in_db': reels.count(),
        'total_plays': total_plays,
        'total_likes': total_likes,
        'total_comments': total_comments,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def instagram_profile_reels(request, profile_id):
    """Danh sách reels của 1 Instagram profile.

    GET /api/scraper/instagram/profiles/<id>/reels/?page=1&page_size=24&sort=plays
    """
    try:
        profile = InstagramProfile.objects.get(id=profile_id)
    except InstagramProfile.DoesNotExist:
        return Response({'error': 'Profile not found'}, status=404)

    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 24)))
    sort = params.get('sort', 'date')
    q = params.get('q', '').strip()
    min_plays = _parse_int(params.get('min_plays'))

    qs = InstagramReel.objects.filter(profile=profile)

    if min_plays is not None:
        qs = qs.filter(play_count__gte=min_plays)

    if sort == 'plays':
        qs = qs.order_by('-play_count')
    elif sort == 'likes':
        qs = qs.order_by('-likes_count')
    else:
        qs = qs.order_by('-date_posted')

    if q:
        filtered = [r for r in qs if
                    _unaccent_match(r.description, q) or
                    any(q.lower().strip('#') in h.lower() for h in (r.hashtags or []))]
    else:
        filtered = list(qs)

    total = len(filtered)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = filtered[start:start + page_size]

    results = [
        {
            'post_id': r.post_id,
            'shortcode': r.shortcode,
            'url': r.url,
            'description': r.description,
            'hashtags': r.hashtags,
            'thumbnail_url': r.thumbnail_drive_url or r.thumbnail_url or '',
            'duration_seconds': r.duration_seconds,
            'is_paid_partnership': r.is_paid_partnership,
            'play_count': r.play_count,
            'likes_count': r.likes_count,
            'comments_count': r.comments_count,
            'date_posted': r.date_posted,
            'profile': {
                'id': profile.id,
                'username': profile.username,
                'avatar_url': profile.avatar_url or '',
            },
        }
        for r in paginated
    ]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'reels': results,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def instagram_profile_toggle(request, profile_id):
    """Toggle bookmark / tracked cho 1 Instagram profile.

    POST /api/scraper/instagram/profiles/<id>/toggle/
    Body: { "field": "is_bookmarked" | "is_tracked" }
    """
    try:
        profile = InstagramProfile.objects.get(id=profile_id)
    except InstagramProfile.DoesNotExist:
        return Response({'error': 'Profile not found'}, status=404)

    field = request.data.get('field', '')
    if field not in ('is_bookmarked', 'is_tracked'):
        return Response({'error': 'field must be is_bookmarked or is_tracked'}, status=400)

    current = getattr(profile, field)
    setattr(profile, field, not current)
    profile.save(update_fields=[field, 'updated_at'])

    return Response({
        'status': 'ok',
        field: not current,
    })


# ═══════════════════════════════════════════════════════════
#  DOUYIN — Keyword search + videos list
# ═══════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def douyin_keyword_search(request):
    """Trigger Douyin keyword search (async via Celery).

    POST /api/scraper/douyin/search/
    Body: { "keyword": "robot", "num_of_posts": 30 }
    """
    keyword = (request.data.get('keyword', '') or '').strip()
    if not keyword:
        return Response({'error': 'keyword is required'}, status=400)

    num = min(200, max(1, int(request.data.get('num_of_posts', 30) or 30)))

    from ..tasks import search_douyin_keyword_task
    search_douyin_keyword_task.delay(keyword, num_of_posts=num)

    return Response({
        'status': 'ok',
        'message': f'Đang tìm kiếm "{keyword}" trên Douyin...',
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def douyin_videos_list(request):
    """Danh sách Douyin videos với pagination + search + filter.

    GET /api/scraper/douyin/videos/?q=robot&page=1&page_size=24&sort=date
    """
    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 24)))
    q = params.get('q', '').strip()
    min_digg = _parse_int(params.get('min_digg'))
    date_from = params.get('date_from', '').strip()
    date_to = params.get('date_to', '').strip()
    sort = params.get('sort', 'date')
    kw_filter = params.get('search_keyword', '').strip()

    qs = DouyinVideo.objects.all()

    if min_digg is not None:
        qs = qs.filter(digg_count__gte=min_digg)
    if date_from:
        qs = qs.filter(date_posted__date__gte=date_from)
    if date_to:
        qs = qs.filter(date_posted__date__lte=date_to)
    if kw_filter:
        qs = qs.filter(search_keyword__icontains=kw_filter)

    if sort == 'likes':
        qs = qs.order_by('-digg_count')
    else:
        qs = qs.order_by('-date_posted')

    if q:
        filtered = [v for v in qs if
                    _unaccent_match(v.description, q) or
                    _unaccent_match(v.search_keyword, q) or
                    any(q.lower().strip('#') in h.lower() for h in (v.hashtags or []))]
    else:
        filtered = list(qs)

    total = len(filtered)
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    start = (page_num - 1) * page_size
    paginated = filtered[start:start + page_size]

    results = [
        {
            'post_id': v.post_id,
            'url': v.url,
            'description': v.description,
            'hashtags': v.hashtags,
            'preview_image': v.preview_image,
            'video_duration': v.video_duration,
            'region': v.region,
            'digg_count': v.digg_count,
            'comment_count': v.comment_count,
            'share_count': v.share_count,
            'collect_count': v.collect_count,
            'music_title': v.music_title,
            'search_keyword': v.search_keyword,
            'date_posted': v.date_posted,
            'author': {
                'id': v.author_id,
                'username': v.author_username,
                'display_name': v.author_display_name,
                'avatar_url': v.author_avatar,
                'followers': v.author_followers,
                'is_verified': v.author_is_verified,
            },
        }
        for v in paginated
    ]

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'videos': results,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def douyin_keyword_suggest(request):
    """Autocomplete keywords đã cào trên Douyin.

    GET /api/scraper/douyin/keywords/suggest/?q=ro
    """
    from django.db.models import Count

    q = request.query_params.get('q', '').strip()
    agg = (
        DouyinVideo.objects
        .exclude(search_keyword='')
        .values('search_keyword')
        .annotate(count=Count('id'))
        .order_by('-count')[:50]
    )

    if q:
        q_ascii = unidecode(q).lower()
        results = [
            {'keyword': row['search_keyword'], 'count': row['count']}
            for row in agg
            if q_ascii in unidecode(row['search_keyword']).lower()
        ][:10]
    else:
        results = [{'keyword': row['search_keyword'], 'count': row['count']} for row in agg[:10]]

    return Response({'suggestions': results})


# ═══════════════════════════════════════════════════════════
#  DOUYIN PROFILE — Quản lý tài khoản Douyin theo dõi
# ═══════════════════════════════════════════════════════════

def _serialize_douyin_profile(p, videos_in_db: int = 0) -> dict:
    return {
        'id': p.id,
        'sec_user_id': p.sec_user_id,
        'uid': p.uid,
        'username': p.username,
        'nickname': p.nickname,
        'avatar_url': p.avatar_url,
        'biography': p.biography,
        'is_verified': p.is_verified,
        'followers_count': p.followers_count,
        'is_bookmarked': p.is_bookmarked,
        'is_tracked': p.is_tracked,
        'is_owned': p.is_owned,
        'is_initial_scraped': p.is_initial_scraped,
        'last_scraped_at': p.last_scraped_at,
        'scraping_status': p.scraping_status,
        'scrape_error': p.scrape_error,
        'created_at': p.created_at,
        'videos_in_db': videos_in_db,
    }


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def douyin_profile_scrape(request):
    """Tạo/cào tài khoản Douyin theo sec_user_id.

    POST /api/scraper/douyin/profile/scrape/
    Body: { "sec_user_id": "MS4w...", "num_of_posts": 30 }

    - Profile đã tồn tại → dispatch background task (cập nhật) + trả về ngay
    - Profile mới → cào 20 video đầu sync qua TikHub, trả dữ liệu ngay, rồi dispatch task cào thêm
    """
    sec_user_id = (request.data.get('sec_user_id', '') or '').strip()
    if not sec_user_id:
        return Response({'error': 'sec_user_id is required'}, status=400)

    is_owned = bool(request.data.get('is_owned', False))
    num = min(600, max(1, int(request.data.get('num_of_posts', 30) or 30)))

    profile, created = DouyinProfile.objects.get_or_create(
        sec_user_id=sec_user_id,
        defaults={'scraping_status': 'idle', 'is_owned': is_owned},
    )

    # Profile đã tồn tại → dispatch task để cập nhật, navigate ngay tới detail
    if not created:
        update_fields = ['scraping_status', 'scrape_error', 'updated_at']
        if is_owned and not profile.is_owned:
            profile.is_owned = True
            update_fields.append('is_owned')
        profile.scraping_status = 'processing'
        profile.scrape_error = None
        profile.save(update_fields=update_fields)
        from ..tasks import scrape_douyin_profile_task
        scrape_douyin_profile_task.delay(profile.id, num_of_posts=num)
        return Response({
            'status': 'ok',
            'message': f'Đang cập nhật profile Douyin...',
            'profile_id': profile.id,
            'already_exists': True,
        })

    # Profile mới → cào đồng bộ 20 video đầu để hiển thị ngay
    profile.scraping_status = 'processing'
    profile.save(update_fields=['scraping_status', 'updated_at'])

    try:
        from ..services.tikhub_douyin import fetch_douyin_user_videos, ingest_douyin_videos
        videos = fetch_douyin_user_videos(sec_user_id=sec_user_id, count=20)
        if not videos:
            profile.scraping_status = 'idle'
            profile.scrape_error = 'TikHub không trả về video nào'
            profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
            return Response({'error': 'Không tìm thấy video cho sec_user_id này'}, status=404)

        # Cập nhật thông tin profile từ author của video đầu tiên
        first_author = (videos[0] if videos else {}).get('author') or {}
        update_fields = ['scraping_status', 'scrape_error', 'is_initial_scraped', 'last_scraped_at', 'updated_at']

        if first_author:
            for field, src_key in [
                ('uid', 'uid'), ('username', 'unique_id'), ('nickname', 'nickname'),
                ('biography', 'signature'), ('is_verified', 'is_verified'),
            ]:
                val = first_author.get(src_key)
                if val is not None:
                    setattr(profile, field, val)
                    if field not in update_fields:
                        update_fields.append(field)

            avatar_obj = first_author.get('avatar_medium') or first_author.get('avatar_larger') or {}
            avatar_urls = avatar_obj.get('url_list') or []
            if avatar_urls:
                profile.avatar_url = avatar_urls[-1]
                if 'avatar_url' not in update_fields:
                    update_fields.append('avatar_url')

            followers = first_author.get('follower_count') or first_author.get('followers_count')
            if followers is not None:
                profile.followers_count = int(followers)
                if 'followers_count' not in update_fields:
                    update_fields.append('followers_count')

        # Lưu videos vào DB (CDN URL trước, Drive upload chạy async trong ingest_douyin_videos)
        label = f'@{profile.username}' if profile.username else f'@{profile.sec_user_id[:20]}'
        result = ingest_douyin_videos(videos, search_keyword=label)

        profile.is_initial_scraped = True
        profile.last_scraped_at = timezone.now()
        profile.scraping_status = 'idle'
        profile.scrape_error = None
        profile.save(update_fields=update_fields)

        # Dispatch task cào thêm (sẽ update_or_create, không trùng lặp)
        from ..tasks import scrape_douyin_profile_task
        scrape_douyin_profile_task.delay(profile.id, num_of_posts=600)

        profile.refresh_from_db()
        return Response({
            'status': 'ok',
            'message': f'Đã cào {result["created"]} posts đầu cho {label}. Đang tiếp tục cào thêm...',
            'profile_id': profile.id,
            'newly_scraped': True,
            'profile': {
                'id': profile.id,
                'sec_user_id': profile.sec_user_id,
                'username': profile.username,
                'nickname': profile.nickname or '',
                'avatar_url': profile.avatar_url or '',
                'biography': profile.biography or '',
                'is_verified': profile.is_verified,
                'followers_count': profile.followers_count,
                'scraping_status': profile.scraping_status,
            },
            'initial_posts_count': result['created'],
        })
    except Exception as e:
        profile.scraping_status = 'failed'
        profile.scrape_error = str(e)[:500]
        profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
        import logging
        logging.getLogger(__name__).error(f'[DOUYIN-PROFILE] error: {e}', exc_info=True)
        return Response({'error': f'Lỗi khi cào: {str(e)[:200]}'}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def douyin_profiles_list(request):
    """Danh sách profiles Douyin đang theo dõi (paginated).

    GET /api/scraper/douyin/profiles/?page=1&page_size=12&search=foo&sort_by=followers
    """
    from django.db.models import Count, Q

    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(50, max(1, _parse_int(params.get('page_size'), 12)))
    search = params.get('search', '').strip()
    sort_by = params.get('sort_by', 'followers')

    is_owned_param = params.get('is_owned', '').strip()

    qs = DouyinProfile.objects.all()
    if search:
        qs = qs.filter(Q(username__icontains=search) | Q(nickname__icontains=search))
    if is_owned_param == 'true':
        qs = qs.filter(is_owned=True)
    elif is_owned_param == 'false':
        qs = qs.filter(is_owned=False)

    secondary_order = '-created_at' if sort_by == 'recent' else '-followers_count'
    qs = qs.order_by('-is_bookmarked', secondary_order)

    total = qs.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    offset = (page_num - 1) * page_size
    profiles = list(qs[offset: offset + page_size])

    # Video counts từ scraper_douyin_videos (filter theo search_keyword = @username)
    usernames = [f'@{p.username}' for p in profiles if p.username]
    counts_qs = (
        DouyinVideo.objects
        .filter(search_keyword__in=usernames)
        .values('search_keyword')
        .annotate(cnt=Count('id'))
    )
    count_map = {row['search_keyword']: row['cnt'] for row in counts_qs}

    return Response({
        'status': 'ok',
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'profiles': [
            _serialize_douyin_profile(p, count_map.get(f'@{p.username}', 0))
            for p in profiles
        ],
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def douyin_profile_detail(request, pk):
    """Chi tiết một profile Douyin.

    GET /api/scraper/douyin/profiles/<pk>/
    """
    try:
        profile = DouyinProfile.objects.get(pk=pk)
    except DouyinProfile.DoesNotExist:
        return Response({'error': 'Not found'}, status=404)

    label = f'@{profile.username}' if profile.username else ''
    from django.db.models import Sum
    qs = DouyinVideo.objects.filter(search_keyword=label) if label else DouyinVideo.objects.none()
    videos_in_db = qs.count()
    agg = qs.aggregate(
        total_digg=Sum('digg_count'),
        total_comment=Sum('comment_count'),
        total_share=Sum('share_count'),
        total_collect=Sum('collect_count'),
    )
    result = _serialize_douyin_profile(profile, videos_in_db)
    result.update({
        'total_diggs': agg['total_digg'] or 0,
        'total_comments': agg['total_comment'] or 0,
        'total_shares': agg['total_share'] or 0,
        'total_collects': agg['total_collect'] or 0,
    })
    return Response(result)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def douyin_profile_toggle(request, pk):
    """Toggle is_bookmarked hoặc is_tracked.

    POST /api/scraper/douyin/profiles/<pk>/toggle/
    Body: { "field": "is_bookmarked" }
    """
    try:
        profile = DouyinProfile.objects.get(pk=pk)
    except DouyinProfile.DoesNotExist:
        return Response({'error': 'Not found'}, status=404)

    field = request.data.get('field', '')
    if field not in ('is_bookmarked', 'is_tracked'):
        return Response({'error': 'field must be is_bookmarked or is_tracked'}, status=400)

    current = getattr(profile, field)
    setattr(profile, field, not current)
    profile.save(update_fields=[field, 'updated_at'])
    return Response({'status': 'ok', field: not current})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def douyin_profile_videos(request, pk):
    """Videos của một profile Douyin (infinite scroll).

    GET /api/scraper/douyin/profiles/<pk>/videos/?page=1&page_size=24&sort=date
    """
    try:
        profile = DouyinProfile.objects.get(pk=pk)
    except DouyinProfile.DoesNotExist:
        return Response({'error': 'Not found'}, status=404)

    label = f'@{profile.username}' if profile.username else ''
    if not label:
        return Response({'videos': [], 'count': 0, 'page': 1, 'page_size': 24, 'total_pages': 0})

    params = request.query_params
    page_num = max(1, _parse_int(params.get('page'), 1))
    page_size = min(100, max(1, _parse_int(params.get('page_size'), 24)))
    sort = params.get('sort', 'date')
    q = params.get('q', '').strip()
    min_digg = _parse_int(params.get('min_digg'), None)

    from django.db.models import Q as DQ
    qs = DouyinVideo.objects.filter(search_keyword=label)
    if q:
        qs = qs.filter(DQ(description__icontains=q) | DQ(hashtags__contains=[q]))
    if min_digg is not None:
        qs = qs.filter(digg_count__gte=min_digg)
    if sort == 'likes':
        qs = qs.order_by('-digg_count')
    else:
        qs = qs.order_by('-date_posted')

    total = qs.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    offset = (page_num - 1) * page_size
    paginated = list(qs[offset: offset + page_size])

    return Response({
        'count': total,
        'page': page_num,
        'page_size': page_size,
        'total_pages': total_pages,
        'videos': [
            {
                'post_id': v.post_id,
                'url': v.url,
                'description': v.description,
                'hashtags': v.hashtags,
                'preview_image': v.preview_image or '',
                'video_duration': v.video_duration,
                'region': v.region,
                'digg_count': v.digg_count,
                'comment_count': v.comment_count,
                'share_count': v.share_count,
                'collect_count': v.collect_count,
                'music_title': v.music_title,
                'date_posted': v.date_posted,
            }
            for v in paginated
        ],
    })
