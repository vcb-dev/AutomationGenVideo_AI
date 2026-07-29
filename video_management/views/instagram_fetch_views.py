"""Instagram — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi ScraperInstagramProfile/ScraperInstagramReel/
ScraperInstagramProfileMetrics. AI chỉ gọi TikHub + parse dữ liệu, trả JSON thô cho BE tự lưu.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.tikhub_instagram import (
    fetch_user_info, fetch_instagram_reels,
    parse_instagram_user_info, parse_instagram_item_user, parse_instagram_reels,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_instagram_profile_reels(request):
    """Fetch user info + reels theo username, fetch + parse only.

    Body: { "username": "...", "count": 600 }
    """
    data = request.data or {}
    username = (data.get('username') or '').strip()
    if not username:
        return Response({'error': 'username is required'}, status=400)

    count = int(data.get('count') or 600)

    user_info = fetch_user_info(username)
    full_profile = parse_instagram_user_info(username, user_info) if user_info else None

    raw_reels = fetch_instagram_reels(username=username, count=count)
    reels = parse_instagram_reels(raw_reels)

    # fallback_user lấy từ reel mới nhất sau khi đã parse+sort — dùng lại item gốc
    # tương ứng (parse_instagram_reels đã sắp xếp theo taken_at giảm dần nội bộ,
    # nên fallback tính trực tiếp từ raw_reels theo cùng logic sắp xếp).
    fallback_user = None
    if raw_reels:
        sorted_raw = sorted(
            raw_reels,
            key=lambda r: r.get('taken_at') if isinstance(r.get('taken_at'), (int, float)) else 0,
            reverse=True,
        )
        fallback_user = parse_instagram_item_user(sorted_raw[0])

    return Response({'full_profile': full_profile, 'fallback_user': fallback_user, 'reels': reels})
