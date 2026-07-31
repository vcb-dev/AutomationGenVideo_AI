"""Facebook owned-pages — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi ManagedFacebookPage/OwnedVideoContent.
AI chỉ gọi Facebook Graph API + parse dữ liệu, trả JSON thô cho BE tự lưu.

page_access_token luôn được truyền qua lại dưới dạng CHUỖI ĐÃ MÃ HÓA (Fernet).
BE không bao giờ tự mã hóa/giải mã — chỉ lưu/forward nguyên chuỗi mà AI trả về.
AI là nơi duy nhất giữ FERNET_KEY và biết cách mã hóa/giải mã token.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.facebook_graph_service import FacebookGraphService
from ..utils.encryption import TokenEncryption


def _decrypt_token(encrypted: str) -> str:
    """Giải mã token đã mã hóa; trả '' nếu rỗng hoặc lỗi."""
    if not encrypted:
        return ''
    if not encrypted.startswith('gAAAAAB'):
        # Chưa mã hóa (hiếm khi xảy ra) — coi như plaintext
        return encrypted
    try:
        return TokenEncryption.decrypt(encrypted)
    except Exception:
        return ''


def _merge_batch_metrics(videos: list, graph: FacebookGraphService) -> list:
    """Gọi batch API lấy view/like/comment/share thật, merge vào từng video (chia nhóm 50 id)."""
    video_ids = [v.get('id') for v in videos if v.get('id')]
    metrics_map = {}
    batch_size = 50
    for i in range(0, len(video_ids), batch_size):
        chunk = video_ids[i:i + batch_size]
        metrics_map.update(graph.update_video_views_batch(chunk))

    merged = []
    for v in videos:
        post_id = v.get('id')
        if not post_id:
            continue

        likes = v.get('like_count', 0)
        comments = v.get('comment_count', 0)
        shares = v.get('share_count', 0)
        views = 0
        raw_json = v.get('raw_data') or v

        if post_id in metrics_map:
            m = metrics_map[post_id]
            views = m.get('view_count', 0)
            likes = m.get('like_count', likes)
            comments = m.get('comment_count', comments)
            shares = m.get('share_count', shares)
            if isinstance(raw_json, dict):
                raw_json['_metrics'] = m.get('raw_json', {})

        video_url = v.get('video_url') or v.get('download_url') or ''
        if not video_url:
            raw_original = v.get('raw_data') or {}
            for att in raw_original.get('attachments', {}).get('data', []):
                src = att.get('media', {}).get('source', '')
                if src:
                    video_url = src
                    break

        merged.append({
            'post_id': str(post_id),
            'caption': v.get('description') or v.get('title') or '',
            'published_at': v.get('created_time') or '',
            'permalink_url': v.get('url') or '',
            'thumbnail_url': v.get('thumbnail_url') or v.get('thumbnail') or '',
            'video_url': video_url,
            'view_count': views,
            'like_count': likes,
            'comment_count': comments,
            'share_count': shares,
            'raw_data': raw_json,
        })
    return merged


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_managed_pages(request):
    """GĐ0: Gọi /me/accounts, trả list Page thô. Token của từng Page được mã hóa trước khi trả về.

    Body: { "user_access_token": "..." } (optional — mặc định dùng token hệ thống trong .env)
    """
    user_token = (request.data or {}).get('user_access_token')
    graph = FacebookGraphService()

    token_to_use = user_token or graph.access_token
    if not token_to_use:
        return Response({'error': 'Không tìm thấy FACEBOOK_ACCESS_TOKEN'}, status=400)

    raw_pages = graph.get_my_managed_pages(token_to_use)
    if not raw_pages:
        return Response({'pages': []})

    pages = []
    for p in raw_pages:
        page_id = p.get('id')
        if not page_id:
            continue
        plaintext_token = p.get('access_token') or ''
        try:
            encrypted_token = TokenEncryption.encrypt(plaintext_token) if plaintext_token else ''
        except Exception as e:
            return Response({'error': f'Mã hóa token thất bại: {e}'}, status=500)

        # Lấy luôn fan_count/followers_count mới nhất bằng token riêng của Page
        # (khớp hành vi cũ: import xong tự làm tươi số liệu ngay)
        followers_count = 0
        likes_count = 0
        if plaintext_token:
            details = graph.get_page_details(page_id, fields="fan_count,followers_count", access_token=plaintext_token)
            if details:
                likes_count = details.get('fan_count', 0) or 0
                followers_count = details.get('followers_count', 0) or 0

        pages.append({
            'page_id': str(page_id),
            'name': p.get('name', ''),
            'username': p.get('username') or str(page_id),
            'category': p.get('category', ''),
            'avatar_url': (p.get('picture') or {}).get('data', {}).get('url', ''),
            'followers_count': followers_count,
            'likes_count': likes_count,
            'page_access_token_encrypted': encrypted_token,
            'raw_data': p,
        })

    return Response({'pages': pages})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_page_sync(request):
    """GĐ2 (+ trigger thủ công): metadata + N bài mới nhất của 1 page, kèm metrics thật.

    Body: { "page_id": "...", "page_access_token_encrypted": "...", "max_posts": 10 }
    """
    data = request.data or {}
    page_id = data.get('page_id')
    if not page_id:
        return Response({'error': 'page_id is required'}, status=400)

    max_posts = int(data.get('max_posts') or 10)
    token = _decrypt_token(data.get('page_access_token_encrypted') or '')

    graph = FacebookGraphService()
    if token:
        graph.access_token = token

    try:
        meta = graph.get_page_metadata(page_id)
    except Exception as e:
        return Response({'error': f'Lỗi lấy metadata: {e}'}, status=502)

    raw_posts = graph.get_page_posts(page_id, max_results=max_posts, access_token=token or None)
    video_posts = [p for p in raw_posts if p.get('is_video') is True]
    videos = _merge_batch_metrics(video_posts, graph) if video_posts else []

    return Response({
        'page_metadata': {
            'page_id': str(meta.get('page_id') or page_id),
            'name': meta.get('name', ''),
            'username': meta.get('page_id') if not meta.get('username') else meta.get('username'),
            'category': meta.get('category', ''),
            'avatar_url': meta.get('picture_url', ''),
            'followers_count': meta.get('followers_count', 0) or 0,
            'likes_count': meta.get('fan_count', 0) or 0,
            'raw_data': meta,
        },
        'videos': videos,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_page_backfill(request):
    """GĐ1 (+ trigger thủ công): cào sâu lịch sử 1 page (lần theo paging.next), kèm metrics thật.

    Body: { "page_id": "...", "page_access_token_encrypted": "...", "max_total": 300 }
    """
    data = request.data or {}
    page_id = data.get('page_id')
    if not page_id:
        return Response({'error': 'page_id is required'}, status=400)

    max_total = int(data.get('max_total') or 300)
    token = _decrypt_token(data.get('page_access_token_encrypted') or '')
    if not token:
        return Response({'error': 'Không giải mã được token'}, status=400)

    graph = FacebookGraphService()
    graph.access_token = token

    raw_posts = graph.get_page_posts_deep(
        page_id=page_id, max_total=max_total, page_size=100, cooldown=1.0, access_token=token,
    )
    video_posts = [p for p in raw_posts if p.get('is_video')]
    videos = _merge_batch_metrics(video_posts, graph) if video_posts else []

    return Response({'videos': videos, 'total_scanned': len(raw_posts)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_resolve_owner(request):
    """Tra chủ sở hữu (Page) thật của 1 object_id Graph API bất kỳ (post/video/reel).

    Dùng khi user dán link Reels công khai (facebook.com/reel/{id}) hoặc link
    ?v={id} — các dạng URL không mang page handle trong path nên không tra được
    page bằng cách parse chuỗi (id hiển thị trên link Reels và post_id nội bộ
    Graph API là 2 định danh khác nhau, không suy ra được cái này từ cái kia).
    Field 'from' của 1 object công khai đọc được bằng token BẤT KỲ còn hiệu lực,
    không nhất thiết phải là token của đúng page sở hữu object đó.

    Body: { "object_id": "...", "page_access_token_encrypted": "..." } (token
    optional — nếu thiếu/giải mã lỗi thì dùng token mặc định của hệ thống)
    """
    data = request.data or {}
    object_id = data.get('object_id')
    if not object_id:
        return Response({'error': 'object_id is required'}, status=400)

    token = _decrypt_token(data.get('page_access_token_encrypted') or '')

    graph = FacebookGraphService()
    details = graph.get_page_details(object_id, fields='from,permalink_url', access_token=token or None)
    if not details:
        return Response({'from_id': None, 'from_name': None, 'permalink_url': None})

    from_obj = details.get('from') or {}
    return Response({
        'from_id': from_obj.get('id'),
        'from_name': from_obj.get('name'),
        'permalink_url': details.get('permalink_url'),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_video_metrics_refresh(request):
    """Refresh view/like/comment cho 1 batch ID Video/Reels NODE THUẦN (không phải Page
    Post ID — xem fetch_metrics_refresh() cho trường hợp đó). Dùng cho link Reels công
    khai (/reel/{id}) user dán tay mà không tra được post_id nội bộ đã sync — object đó
    là 1 Video node, KHÔNG hỗ trợ field shares/reactions/insights như Page Post.

    Body: { "page_access_token_encrypted": "...", "video_ids": ["...", ...] }
    """
    data = request.data or {}
    video_ids = data.get('video_ids') or []
    if not video_ids:
        return Response({'metrics': {}})

    token = _decrypt_token(data.get('page_access_token_encrypted') or '')
    if not token:
        return Response({'error': 'Không giải mã được token'}, status=400)

    graph = FacebookGraphService()
    metrics_map = graph.update_video_node_metrics_batch(video_ids, access_token=token)

    metrics = {
        vid: {
            'view_count': m.get('view_count', 0),
            'like_count': m.get('like_count', 0),
            'comment_count': m.get('comment_count', 0),
            'share_count': m.get('share_count', 0),
        }
        for vid, m in metrics_map.items()
    }
    return Response({'metrics': metrics})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_metrics_refresh(request):
    """GĐ3: refresh view/like/comment/share cho 1 batch post_id cụ thể (đã biết token của page).

    Body: { "page_access_token_encrypted": "...", "post_ids": ["...", ...] }
    """
    data = request.data or {}
    post_ids = data.get('post_ids') or []
    if not post_ids:
        return Response({'metrics': {}})

    token = _decrypt_token(data.get('page_access_token_encrypted') or '')
    if not token:
        return Response({'error': 'Không giải mã được token'}, status=400)

    graph = FacebookGraphService()
    graph.access_token = token

    metrics_map = {}
    batch_size = 50
    for i in range(0, len(post_ids), batch_size):
        chunk = post_ids[i:i + batch_size]
        metrics_map.update(graph.update_video_views_batch(chunk))

    metrics = {
        pid: {
            'view_count': m.get('view_count', 0),
            'like_count': m.get('like_count', 0),
            'comment_count': m.get('comment_count', 0),
            'share_count': m.get('share_count', 0),
        }
        for pid, m in metrics_map.items()
    }
    return Response({'metrics': metrics})
