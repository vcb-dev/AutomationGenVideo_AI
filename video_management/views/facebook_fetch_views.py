"""Facebook owned-pages — fetch-only endpoints (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi ManagedFacebookPage/OwnedVideoContent.
AI chỉ gọi Facebook Graph API + parse dữ liệu, trả JSON thô cho BE tự lưu.

page_access_token luôn được truyền qua lại dưới dạng CHUỖI ĐÃ MÃ HÓA (Fernet).
BE không bao giờ tự mã hóa/giải mã — chỉ lưu/forward nguyên chuỗi mà AI trả về.
AI là nơi duy nhất giữ FERNET_KEY và biết cách mã hóa/giải mã token.
"""

import logging
import os
import re
import subprocess
import tempfile
import time
import uuid

import requests
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..services.facebook_graph_service import FacebookGraphService
from ..services import facebook_token_store
from ..utils.encryption import TokenEncryption

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_token_refresh(request):
    """Gia hạn User Access Token nếu sắp hết hạn. Cron bên BE gọi endpoint này mỗi ngày.

    Không nhận tham số — token nguồn lấy từ token store phía AI (nơi duy nhất giữ nó).
    """
    return Response(facebook_token_store.refresh_user_token())


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_token_save(request):
    """Lưu User Access Token mới sau khi người dùng cấp quyền (BE gọi từ luồng OAuth).

    Body: { "access_token": "...", "expires_in": 5184000 }

    Vì sao cần: quyền Facebook đóng cứng vào token lúc phát hành. App được duyệt thêm quyền
    KHÔNG làm token cũ mạnh lên, và `fb_exchange_token` chỉ đổi hạn chứ không thêm quyền (đo
    ngày 16/08/2026: token vừa gia hạn vẫn thiếu `instagram_manage_insights`). Đường duy nhất
    để có quyền mới là đi qua màn hình đồng ý — tức luồng OAuth bên BE. Trước đây BE đổi được
    token mới rồi VỨT ĐI, nên cấp quyền xong hệ thống vẫn chạy bằng token cũ.

    AI là nơi duy nhất giữ token store (.fb_token.json), nên BE gửi sang đây thay vì tự ghi file.
    """
    token = str((request.data or {}).get('access_token') or '').strip()
    if not token:
        return Response({'error': 'access_token is required'}, status=400)

    # Mặc định 60 ngày — đúng hạn mà fb_exchange_token trả về cho long-lived token.
    expires_in = int((request.data or {}).get('expires_in') or 5_184_000)
    facebook_token_store.save_token(token, expires_in)
    logger.info('[TOKEN] Đã lưu User Access Token mới từ luồng OAuth, hạn %d ngày', expires_in // 86_400)
    return Response({'status': 'ok', 'days': expires_in // 86_400})


def _get_ffmpeg_path() -> str:
    """Cùng cách tìm với transcribe_views: ưu tiên FFMPEG_PATH trong .env rồi mới tới PATH."""
    from django.conf import settings
    import shutil
    p = str(getattr(settings, 'FFMPEG_PATH', '')).strip()
    if p and os.path.isfile(p):
        return p
    return shutil.which('ffmpeg') or ''


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


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_video_source(request):
    """
    Lấy link phát video CÒN HẠN của một bài đăng thuộc trang mình quản lý.

    Body: { "page_access_token_encrypted": "...", "post_id": "<page_id>_<post_id>" }
    Trả:  { "success": true, "video_url": "https://scontent…fbcdn.net/…" }

    ── Vì sao cần endpoint này ─────────────────────────────────────────────────
    Cột `video_url` lưu lúc cào là link fbcdn CÓ CHỮ KÝ và hết hạn sau ít ngày — đo trên
    video mới nhất (đăng 02/08, tải ngày 05/08) đã trả HTTP 403. Còn `permalink_url`
    (facebook.com/reel/…) thì yt-dlp không tải nổi vì Facebook đòi đăng nhập: thử thật chỉ
    tải về 112 KB trang đăng nhập rồi bỏ cuộc sau 4 phút 14 giây.

    Trang là của chính công ty và token đã có sẵn, nên hỏi lại Graph API là cách duy nhất
    vừa đúng vừa chắc. Link trả về cũng có hạn, nên phải dùng NGAY (tải để bóc lời thoại),
    không được lưu lại dùng sau.
    """
    encrypted = (request.data.get('page_access_token_encrypted') or '').strip()
    post_id = (request.data.get('post_id') or '').strip()
    if not post_id:
        return Response({'success': False, 'error': 'post_id is required'}, status=400)

    token = _decrypt_token(encrypted)
    if not token:
        return Response({'success': False, 'error': 'Không giải mã được token'}, status=400)

    graph = FacebookGraphService()
    graph.access_token = token

    try:
        resp = requests.get(
            f"{graph.BASE_URL}/{post_id}",
            params={
                # Cùng bộ field mà luồng cào đang dùng, để _extract_video_source() bóc được y hệt.
                'fields': 'full_picture,attachments{type,media_type,media,subattachments}',
                'access_token': token,
            },
            timeout=20,
        )
    except requests.exceptions.RequestException as e:
        return Response({'success': False, 'error': f'Lỗi gọi Graph API: {e}'}, status=502)

    if resp.status_code != 200:
        return Response(
            {'success': False, 'error': f'Graph API {resp.status_code}: {resp.text[:200]}'},
            status=502,
        )

    video_url, _thumbnail, is_video = FacebookGraphService._extract_video_source(resp.json())
    if not video_url:
        loi = 'Bài này không phải video' if not is_video else 'Không lấy được link phát của video'
        return Response({'success': False, 'error': loi}, status=404)

    return Response({'success': True, 'video_url': video_url})


def _srt_thanh_van_xuoi(srt: str) -> str:
    """
    Gộp file .srt thành một đoạn văn liền mạch để đưa cho PAAST chấm.

    Mỗi khối .srt gồm 3 phần: số thứ tự, dòng thời gian, rồi mới tới chữ. Bỏ hai phần đầu,
    nối phần chữ lại. Facebook ngắt câu giữa chừng theo khung hình (khối này kết thúc bằng
    "rồi bóp", khối sau bắt đầu bằng "thêm một ít…") nên nối thẳng là ra câu hoàn chỉnh,
    KHÔNG được chèn dấu chấm hay xuống dòng vào giữa.
    """
    doan = []
    for khoi in re.split(r'\r?\n\r?\n', srt.strip()):
        dong = [d for d in re.split(r'\r?\n', khoi.strip()) if d.strip()]
        if len(dong) < 3:
            continue
        doan.append(' '.join(dong[2:]).strip())
    return re.sub(r'\s+', ' ', ' '.join(doan)).strip()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_video_captions(request):
    """
    Lấy PHỤ ĐỀ TỰ SINH của một video thuộc trang mình quản lý, trả về dạng văn xuôi.

    Body: { "page_access_token_encrypted": "...", "post_id": "<page_id>_<post_id>" }
    Trả:  { "success": true, "noi_dung": "...", "ngon_ngu": "vi_VN", "so_ky_tu": 542 }

    ── Vì sao đây là đường lấy kịch bản tốt nhất ───────────────────────────────
    Facebook tự chạy nhận dạng giọng nói cho video của trang và cho tải file .srt về qua
    Graph API. Đo trên video thật: một lệnh gọi + tải 998 byte, mất ~2 giây, ra 542 ký tự
    tiếng Việt đúng nguyên văn lời nói.

    Ba đường còn lại đều đã thử và đắt hơn nhiều:
      - Tải video rồi chạy Whisper: KHÔNG được, `source` của Graph API luôn trả bản chỉ có
        hình (đo 4 video, đều `audio_stream=0`).
      - yt-dlp trên link reel: đòi đăng nhập, tải về 112 KB trang login rồi bỏ cuộc sau 4 phút.
      - OCR chữ trên khung hình: tốn tiền vision API cho mỗi khung, chữ cháy thường chỉ là
        câu hook chứ không phải cả kịch bản.

    Phủ khoảng 6/14 video trong mẫu ngẫu nhiên — video không có phụ đề thì trả 404 để phía
    gọi biết mà hiện "chưa có kịch bản", KHÔNG coi là lỗi hệ thống.
    """
    encrypted = (request.data.get('page_access_token_encrypted') or '').strip()
    post_id = (request.data.get('post_id') or '').strip()
    if not post_id:
        return Response({'success': False, 'error': 'post_id is required'}, status=400)

    token = _decrypt_token(encrypted)
    if not token:
        return Response({'success': False, 'error': 'Không giải mã được token'}, status=400)

    graph = FacebookGraphService()

    def _graph(path: str, fields: str):
        return requests.get(
            f"{graph.BASE_URL}/{path}",
            params={'fields': fields, 'access_token': token} if fields else {'access_token': token},
            timeout=20,
        )

    try:
        # Phụ đề treo ở NODE VIDEO chứ không ở bài đăng, nên phải lấy video id từ attachments trước.
        r = _graph(post_id, 'attachments{target}')
        if r.status_code != 200:
            return Response({'success': False, 'error': f'Graph API {r.status_code}'}, status=502)
        data = (r.json().get('attachments') or {}).get('data') or []
        video_id = (data[0].get('target') or {}).get('id') if data else ''
        if not video_id:
            return Response({'success': False, 'error': 'Bài này không phải video'}, status=404)

        r = _graph(f'{video_id}/captions', '')
        if r.status_code != 200:
            return Response({'success': False, 'error': f'Graph API {r.status_code}'}, status=502)
        tracks = r.json().get('data') or []
        if not tracks:
            return Response({'success': False, 'error': 'Video chưa có phụ đề tự sinh'}, status=404)

        # Ưu tiên tiếng Việt; không có thì lấy track đầu và để phía gọi tự quyết việc dịch.
        track = next((t for t in tracks if str(t.get('locale', '')).startswith('vi')), tracks[0])
        srt = requests.get(track['uri'], timeout=30)
        if srt.status_code != 200:
            return Response({'success': False, 'error': f'Không tải được .srt ({srt.status_code})'}, status=502)

        # PHẢI ép utf-8: Facebook trả .srt không kèm charset trong header, mà `requests` gặp
        # text/* không khai charset thì mặc định đoán ISO-8859-1. Để nguyên thì "Thì đống bụi"
        # ra thành "ThÃ¬ Äá»ng bá»¥i" — hỏng toàn bộ kịch bản tiếng Việt.
        srt.encoding = 'utf-8'
        noi_dung = _srt_thanh_van_xuoi(srt.text)
        if not noi_dung:
            return Response({'success': False, 'error': 'Phụ đề rỗng'}, status=404)

        return Response({
            'success': True,
            'noi_dung': noi_dung,
            'ngon_ngu': track.get('locale', ''),
            'so_ky_tu': len(noi_dung),
        })
    except requests.exceptions.RequestException as e:
        return Response({'success': False, 'error': f'Lỗi mạng: {e}'}, status=502)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fetch_video_transcript(request):
    """
    Bóc LỜI THOẠI của một video nội bộ — dùng khi Facebook chưa có phụ đề tự sinh.

    Body: { "page_id": "...", "video_id": "...", "post_id": "<page>_<post>" }
    Trả:  { "success": true, "noi_dung": "...", "so_ky_tu": 675, "nguon": "whisper" }

    ── Vì sao phải qua RapidAPI ────────────────────────────────────────────────
    Graph API chỉ trả bản video KHÔNG có tiếng (đo 4 video, đều `audio_stream=0`), nên
    Whisper không có gì để nghe. RapidAPI trả `video_files.video_hd_file` là bản MP4 hoàn
    chỉnh — kiểm tra bằng ffmpeg thấy đủ `Stream #0:1 Audio: aac 44100 Hz stereo`.

    Nhận dạng chạy TẠI MÁY (faster-whisper) vì credit OpenAI đã cạn. Xem local_whisper.py.
    """
    data = request.data or {}
    page_id = str(data.get('page_id') or '').strip()
    video_id = str(data.get('video_id') or '').strip()
    post_id = str(data.get('post_id') or '').strip()
    if not page_id or not (video_id or post_id):
        return Response({'success': False, 'error': 'Cần page_id và video_id/post_id'}, status=400)

    from django.conf import settings
    from ..services import local_whisper

    if not local_whisper.san_sang():
        return Response({'success': False, 'error': 'Chưa cài faster-whisper trên máy chủ AI'}, status=500)

    key = getattr(settings, 'RAPIDAPI_FACEBOOK_KEY', '')
    host = getattr(settings, 'RAPIDAPI_FACEBOOK_HOST', '')
    if not key:
        return Response({'success': False, 'error': 'Chưa cấu hình RAPIDAPI_FACEBOOK_KEY'}, status=500)

    # post_id của ta là "<page>_<post>", còn RapidAPI chỉ trả phần sau dấu gạch dưới.
    duoi_post = post_id.split('_')[-1] if post_id else ''
    permalink = str(data.get('permalink') or '').strip()
    headers_rapid = {'x-rapidapi-key': key, 'x-rapidapi-host': host}
    link_video = ''

    def _bat_link_video(o):
        """Bới sâu tìm link .mp4 — mỗi nhà cung cấp chôn nó ở một chỗ khác nhau."""
        if isinstance(o, dict):
            for k, v in o.items():
                if (
                    isinstance(v, str)
                    and v.startswith('http')
                    and ('video_hd_file' in k or 'video_sd_file' in k
                         or 'browser_native_hd_url' in k or 'browser_native_sd_url' in k)
                ):
                    return v
                found = _bat_link_video(v)
                if found:
                    return found
        elif isinstance(o, list):
            for v in o:
                found = _bat_link_video(v)
                if found:
                    return found
        return ''

    # ── Cách 0: Apify ─────────────────────────────────────────────────────────
    # Rẻ hơn RapidAPI nhiều nên thử TRƯỚC: đo thật một lần chạy tốn 0,0125 USD, mà gói FREE
    # cho 5 USD/tháng → khoảng 400 video/tháng. RapidAPI chỉ 200 LƯỢT GỌI/tháng mà mỗi video
    # tốn 1–4 lượt, tức chỉ 50–200 video. Cả hai đều trả bản video CÓ TIẾNG.
    apify_token = str(getattr(settings, 'APIFY_API_TOKEN', '') or os.environ.get('APIFY_API_TOKEN', '')).strip()
    if permalink and apify_token:
        try:
            r = requests.post(
                'https://api.apify.com/v2/acts/apify~facebook-posts-scraper/'
                f'run-sync-get-dataset-items?token={apify_token}&timeout=180',
                json={'startUrls': [{'url': permalink}], 'resultsLimit': 1},
                timeout=240,
            )
            if r.status_code in (200, 201):
                link_video = _bat_link_video(r.json())
                if not link_video:
                    logger.warning('[Transcript] Apify chạy được nhưng không có link video — thử RapidAPI')
            else:
                # Hết credit tháng thì Apify trả mã lỗi chứ KHÔNG ném ngoại lệ, nên nhánh này
                # phải tự log. Thiếu nó thì lúc Apify cạn, hệ thống âm thầm chuyển sang
                # RapidAPI và đốt nốt 200 lượt/tháng mà không ai biết.
                logger.warning(
                    '[Transcript] Apify trả %s (%s) — chuyển sang RapidAPI',
                    r.status_code, r.text[:120],
                )
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.warning('[Transcript] Apify lỗi, chuyển sang RapidAPI: %s', e)

    try:
        # ── Cách 1: tra THẲNG theo link bài ────────────────────────────────────
        # Nhanh và chắc hơn hẳn quét danh sách: video đăng 40 ngày trước nằm sâu quá 5 trang
        # nên cách quét trả 404, đo được trên video thật.
        # Endpoint này hay trả RETRY_ERROR ở lần gọi đầu nên phải thử lại vài lượt.
        if permalink and not link_video:
            for _ in range(4):
                r = requests.get(f'https://{host}/get_facebook_post_details',
                                 headers=headers_rapid, params={'link': permalink}, timeout=60)
                if r.status_code == 200 and '"success":false' not in r.text:
                    link_video = _bat_link_video(r.json())
                    break
                time.sleep(3)

        # ── Cách 2: quét danh sách reel của trang ──────────────────────────────
        # Chỉ dùng khi cách 1 không ra; hiệu quả với video mới đăng.
        cursor = ''
        for _ in range(5):
            if link_video:
                break
            r = requests.get(
                f'https://{host}/get_facebook_reels_details',
                headers=headers_rapid,
                params={'link': f'https://www.facebook.com/{page_id}', 'cursor': cursor},
                timeout=60,
            )
            if r.status_code != 200:
                break
            data_r = (r.json() or {}).get('data') or {}
            for reel in data_r.get('reels') or []:
                if (video_id and str(reel.get('video_id')) == video_id) or (
                    duoi_post and str(reel.get('post_id')) == duoi_post
                ):
                    vf = reel.get('video_files') or {}
                    link_video = vf.get('video_hd_file') or vf.get('video_sd_file') or ''
                    break
            cursor = ((data_r.get('page_info') or {}).get('end_cursor')) or ''
            if not cursor:
                break
    except requests.exceptions.RequestException as e:
        return Response({'success': False, 'error': f'RapidAPI lỗi: {e}'}, status=502)

    if not link_video:
        return Response({'success': False, 'error': 'Không tìm thấy video này trên RapidAPI'}, status=404)

    ffmpeg = _get_ffmpeg_path()
    if not ffmpeg:
        return Response({'success': False, 'error': 'Không tìm thấy FFmpeg'}, status=500)

    uid = uuid.uuid4().hex[:8]
    tmp = tempfile.gettempdir()
    mp4 = os.path.join(tmp, f'vcb_t_{uid}.mp4')
    mp3 = os.path.join(tmp, f'vcb_t_{uid}.mp3')
    try:
        with requests.get(link_video, stream=True, timeout=240) as resp:
            resp.raise_for_status()
            with open(mp4, 'wb') as f:
                for chunk in resp.iter_content(1 << 20):
                    f.write(chunk)

        # 16kHz mono là đúng thứ Whisper cần; để nguyên stereo 44.1kHz chỉ làm file to gấp
        # mấy lần mà không tăng độ chính xác.
        subprocess.run(
            [ffmpeg, '-i', mp4, '-vn', '-ac', '1', '-ar', '16000',
             '-acodec', 'libmp3lame', '-q:a', '4', '-y', mp3],
            capture_output=True, timeout=300,
        )
        if not os.path.exists(mp3) or os.path.getsize(mp3) < 500:
            return Response({'success': False, 'error': 'Video này không có tiếng'}, status=404)

        # BE báo trang này có phải trang tiếng Việt không — quyết định có dùng bộ từ điển
        # tiếng Việt hay để Whisper tự nhận dạng.
        tieng_viet = bool(data.get('tieng_viet', True))
        noi_dung, ngon_ngu = local_whisper.boc_loi_thoai(mp3, tieng_viet=tieng_viet)
        if not noi_dung:
            return Response({'success': False, 'error': 'Không bóc được lời thoại'}, status=502)

        return Response({
            'success': True,
            'noi_dung': noi_dung,
            'so_ky_tu': len(noi_dung),
            'ngon_ngu': ngon_ngu,
            'nguon': 'whisper',
        })
    except Exception as e:
        return Response({'success': False, 'error': f'Lỗi xử lý video: {e}'}, status=502)
    finally:
        for p in (mp4, mp3):
            try:
                os.remove(p)
            except Exception:
                pass
