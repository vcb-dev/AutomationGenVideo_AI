"""Facebook external/competitor fanpages — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi scraper_fanpages/scraper_facebook_reels/
scraper_fanpage_metrics_history. AI chỉ gọi RapidAPI + parse dữ liệu, trả JSON
thô cho BE tự lưu.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.rapidapi_facebook import (
    fetch_page_profile, fetch_reels_only, parse_fanpage_profile, parse_facebook_reels,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_facebook_page_reels(request):
    """Fetch profile + reels cho 1 fanpage, fetch + parse only.

    Luôn thử fetch cả profile lẫn reels bất kể profile API có thành công hay không
    (khớp hành vi scrape_reels_sync cũ — dùng cho cả periodic lẫn manual trigger,
    caller tự quyết định coi profile_api_ok=False là lỗi hay không).

    - profile_api_ok: RapidAPI profile-detail call có trả dữ liệu hay không (raw).
    - profile: dict đã parse+merge (ưu tiên profile API, fallback author trong reel
      đầu tiên nếu profile API fail) — None nếu không resolve được profile_id nào cả.

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
    )

    parsed_profile = None
    if profile or reels_raw:
        first_reel = reels_raw[0] if reels_raw else {}
        parsed_profile = parse_fanpage_profile(profile, first_reel)

    parsed_reels = parse_facebook_reels(reels_raw)
    return Response({
        'profile_api_ok': profile is not None,
        'profile': parsed_profile,
        'reels': parsed_reels,
    })
