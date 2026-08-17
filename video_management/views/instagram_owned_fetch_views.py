"""Instagram NỘI BỘ (owned) — endpoint fetch-only, đọc qua Graph API.

Khác `instagram_fetch_views.py` ngay bên cạnh: file kia cào Instagram BÊN NGOÀI theo username
qua TikHub (tính tiền mỗi lượt gọi). File này đọc tài khoản Instagram Business của chính công
ty, bằng page token của Facebook Page mà nó nối vào — miễn phí, không hạn mức trả phí.

Cùng khuôn với facebook_fetch_views.py: AI chỉ gọi Graph API + parse, BE là nơi duy nhất ghi
vào ScraperInstagramProfile / ScraperInstagramReel.

page_access_token luôn truyền qua lại dưới dạng CHUỖI ĐÃ MÃ HOÁ (Fernet). BE không bao giờ tự
mã hoá/giải mã — AI giữ FERNET_KEY và là nơi duy nhất biết cách.
"""

import logging

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.instagram_graph_service import InstagramGraphService
from ..utils.encryption import TokenEncryption

logger = logging.getLogger(__name__)

# Trần số bài lấy trong một lượt.
MAX_LIMIT = int(getattr(settings, 'INSTAGRAM_OWNED_FETCH_MAX_LIMIT', 50))
DEFAULT_LIMIT = int(getattr(settings, 'INSTAGRAM_OWNED_FETCH_DEFAULT_LIMIT', 25))


def _decrypt_token(encrypted: str) -> str:
    """Giải mã token đã mã hoá; trả '' nếu rỗng hoặc lỗi. Giống _decrypt_token bên Facebook."""
    if not encrypted:
        return ''
    if not encrypted.startswith('gAAAAAB'):
        # Chưa mã hoá (hiếm) — coi như plaintext.
        return encrypted
    try:
        return TokenEncryption.decrypt(encrypted)
    except Exception:
        return ''


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_owned_account(request):
    """Tài khoản Instagram Business gắn với một Facebook Page.

    Body: { "page_id": "...", "page_access_token_encrypted": "..." }
    Trả:  { "account": {...} } hoặc { "account": null } nếu page không nối Instagram.

    `account = null` KHÔNG phải lỗi: đo thật 16/08/2026 có 11/25 page không nối Instagram.
    """
    data = request.data or {}
    page_id = str(data.get('page_id') or '').strip()
    if not page_id:
        return Response({'error': 'page_id is required'}, status=400)

    token = _decrypt_token(data.get('page_access_token_encrypted') or '')
    if not token:
        return Response({'error': 'Không giải mã được token'}, status=400)

    return Response({'account': InstagramGraphService().fetch_owned_account(page_id, token)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_media(request):
    """Bài đăng gần nhất của một tài khoản Instagram, đã chuẩn hoá theo cột của bảng reel.

    Body: { "instagram_id": "...", "page_access_token_encrypted": "...", "limit": 25 }
    Trả:  { "media": [ { post_id, shortcode, url, description, hashtags, likes_count,
                         comments_count, view_count, date_posted, ... } ] }

    `view_count = null` nghĩa là KHÔNG LẤY ĐƯỢC (thiếu quyền instagram_manage_insights, hoặc
    bài là ảnh), khác hẳn 0 nghĩa là không ai xem — BE phải giữ nguyên số cũ khi gặp null.
    """
    data = request.data or {}
    instagram_id = str(data.get('instagram_id') or '').strip()
    if not instagram_id:
        return Response({'error': 'instagram_id is required'}, status=400)

    token = _decrypt_token(data.get('page_access_token_encrypted') or '')
    if not token:
        return Response({'error': 'Không giải mã được token'}, status=400)

    limit = min(int(data.get('limit') or 25), MAX_LIMIT)
    media = InstagramGraphService().fetch_media(instagram_id, token, limit=limit)
    return Response({'media': media})
