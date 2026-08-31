"""Facebook external/competitor fanpages — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi scraper_fanpages/scraper_facebook_reels/
scraper_fanpage_metrics_history. AI chỉ gọi RapidAPI + parse dữ liệu, trả JSON
thô cho BE tự lưu.
"""

import re

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.rapidapi_facebook import (
    fetch_page_profile, fetch_reels_only, parse_fanpage_profile, parse_facebook_reels,
)

# Handle không phải tên page — không dùng làm định danh fallback được.
_NON_PAGE_HANDLES = {'profile.php', 'watch', 'reel', 'reels', 'groups', 'share'}


def _extract_page_handle(page_url: str) -> str:
    """Trích handle từ URL fanpage. Trả '' nếu URL không chứa tên page."""
    m = re.search(r'facebook\.com/([^/?&#]+)', page_url or '')
    handle = m.group(1) if m else ''
    return '' if handle in _NON_PAGE_HANDLES else handle


def _fallback_from_cache(handle: str) -> dict:
    """Fallback Cấp 1: metadata đã cache từ lần fetch trước (còn hạn TTL 24h)."""
    try:
        from ..models import FacebookPageCache
        cached = (
            FacebookPageCache.objects
            .filter(username=handle, expires_at__gt=timezone.now())
            .first()
        )
    except Exception:
        return {}
    if not cached:
        return {}
    return {
        'name': cached.page_name or handle,
        'avatar_url': cached.avatar_url or '',
        'followers_count': int(cached.followers_count or 0),
    }


def _build_fallback_profile(page_url: str) -> dict:
    """Dựng profile tạm khi RapidAPI không trả được gì, để BE vẫn tạo/giữ được kênh.

    profile_id phải mang tiền tố 'tmp_' — đó là dấu hiệu BE dùng để nhận biết bản
    ghi tạm và ghi đè bằng page_id thật khi RapidAPI hồi phục (xem
    facebook-external-scraper.service.ts::applyFanpageUpdate).

    is_verified để None (không phải False) vì đây là "chưa biết", không phải "không
    có tick" — BE chỉ ghi đè field này khi khác None.
    """
    handle = _extract_page_handle(page_url)
    if not handle:
        return {}

    cached = _fallback_from_cache(handle)
    return {
        'profile_id': f'tmp_{handle}',
        'name': cached.get('name') or handle,
        'page_url': page_url,
        'handle': handle,
        'avatar_url': cached.get('avatar_url', ''),
        'is_verified': None,
        'followers_count': cached.get('followers_count', 0),
    }


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_facebook_page_reels(request):
    """Fetch profile + reels cho 1 fanpage, fetch + parse only.

    Luôn thử fetch cả profile lẫn reels bất kể profile API có thành công hay không
    (khớp hành vi scrape_reels_sync cũ — dùng cho cả periodic lẫn manual trigger,
    caller tự quyết định coi profile_api_ok=False là lỗi hay không).

    - profile_api_ok: có lấy được dữ liệu THẬT từ RapidAPI hay không (profile API,
      hoặc author trong reel đầu tiên). False nghĩa là `profile` bên dưới chỉ là dữ
      liệu tạm dựng từ URL/cache — BE phải xử lý khác đi, đừng ghi đè dữ liệu tốt.
    - fallback_used: nghịch đảo của profile_api_ok khi vẫn dựng được profile tạm.
    - profile: dict đã parse+merge — None nếu không resolve được gì, kể cả fallback.

    Body: { "page_url": "...", "num_of_posts": 30, "exclude_post_ids": [...], "start_date": "2026-07-01" }
    """
    data = request.data or {}
    page_url = (data.get('page_url') or '').strip()
    if not page_url:
        return Response({'error': 'page_url is required'}, status=400)

    num = int(data.get('num_of_posts') or 30)
    exclude_post_ids = data.get('exclude_post_ids') or []
    start_date = (data.get('start_date') or '').strip()

    profile = fetch_page_profile(page_url)
    reels_raw = fetch_reels_only(
        page_url=page_url,
        num_of_posts=num,
        exclude_post_ids=exclude_post_ids,
        start_date=start_date,
        profile=profile,
    )

    # Dữ liệu thật từ RapidAPI: profile API, hoặc author trong reel đầu tiên.
    rapidapi_profile = None
    if profile or reels_raw:
        first_reel = reels_raw[0] if reels_raw else {}
        rapidapi_profile = parse_fanpage_profile(profile, first_reel)

    parsed_profile = rapidapi_profile or _build_fallback_profile(page_url) or None

    parsed_reels = parse_facebook_reels(reels_raw)
    return Response({
        'profile_api_ok': rapidapi_profile is not None,
        'fallback_used': rapidapi_profile is None and parsed_profile is not None,
        'profile': parsed_profile,
        'reels': parsed_reels,
    })
