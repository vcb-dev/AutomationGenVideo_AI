"""
Video Downloader API — tải video/audio từ mạng xã hội bằng yt-dlp.
Hỗ trợ: YouTube, TikTok, Facebook, Instagram, X, Douyin, Bilibili, ...
Dùng cho trang Tiện ích → Tải video (và Chrome extension VCB).

Luồng tải chạy nền (job + polling), lý do:
1. Né timeout ~100s của Cloudflare Tunnel — POST khởi tạo trả về ngay lập tức,
   các lần poll đều ngắn, chỉ request lấy file cuối cùng mới truyền dữ liệu dài.
2. Cho phép hiện % tiến trình tải thật thay vì spinner treo im re.
"""
import os
import re
import sys
import json
import time
import uuid
import glob
import socket
import hashlib
import logging
import ipaddress
import tempfile
import threading
import subprocess
import requests
from urllib.parse import urlparse

from django.conf import settings
from django.http import FileResponse
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.response import Response

from .transcribe_views import _get_ytdlp, _get_ffmpeg
from .mix_progress_store import progress_set, progress_update, progress_get

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 600  # 10 phút cho video dài
INFO_TIMEOUT = 60
MAX_FILESIZE = os.environ.get('VIDEO_DL_MAX_FILESIZE', '3G')  # chặn video/âm thanh quá khổ tốn tài nguyên

# Bấm "Kiểm tra" (video_info) rồi bấm tải chỉ vài giây sau — nếu tái dùng luôn kết quả
# trích xuất đó (--load-info-json) thì job tải khỏi phải trích xuất lại từ đầu (đỡ hẳn
# bước gọi trang/API của nền tảng, phần thường chiếm nhiều giây nhất trước khi có % tải
# thật). TTL ngắn để tránh dùng phải link CDN đã hết hạn chữ ký trên các nền tảng ký URL.
INFO_CACHE_TTL_SECONDS = 90

_DL_MAX_CONCURRENT = 3
_dl_semaphore = threading.Semaphore(_DL_MAX_CONCURRENT)

# Nếu job tải xong nhưng người dùng không bao giờ bấm tải về (đóng tab, mất mạng...),
# _SelfDeletingFile không bao giờ được gọi tới → file nằm mãi trên disk. Dọn cưỡng bức sau 30 phút.
UNCLAIMED_FILE_TTL_SECONDS = 1800

JOB_ID_RE = re.compile(r'^[0-9a-f]{32}$')
_PERCENT_RE = re.compile(r'\[download\]\s+([\d.]+)%')

BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
COMMON_HEADERS = [
    '--add-header', f'User-Agent:{BROWSER_UA}',
    '--add-header', 'Accept-Language:vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
]

# X/Instagram/Facebook chặn kiểu truy cập không giống trình duyệt thật (không cookie/phiên
# đăng nhập) — yt-dlp hay bị chặn ở đúng 3 nền tảng này. Cobalt (github.com/imputnet/cobalt,
# tự host qua cobalt/docker-compose.yml) dùng kỹ thuật "guest token" của chính X để giả lập
# trình duyệt chưa đăng nhập xem nội dung public, được đội ngũ của họ bảo trì sát sao vì chỉ
# tập trung ~20 nền tảng — nên thử Cobalt TRƯỚC cho 3 nền tảng này, yt-dlp làm lưới an toàn
# nếu Cobalt lỗi/chưa chạy. Các nền tảng khác (TikTok, YouTube, Douyin...) không đổi gì.
COBALT_PLATFORM_HOSTS = ('twitter.com', 'x.com', 'instagram.com', 'facebook.com', 'fb.watch')
COBALT_CONNECT_TIMEOUT = 5  # ngắn — tránh delay khi Cobalt chưa chạy/không phản hồi
COBALT_MAX_FILESIZE_BYTES = 3 * 1024 * 1024 * 1024  # 3G, khớp MAX_FILESIZE của yt-dlp


def _cobalt_platform(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or '').lower().lstrip('www.')
        return any(host == h or host.endswith('.' + h) for h in COBALT_PLATFORM_HOSTS)
    except Exception:
        return False


_COBALT_PLATFORM_LABELS = {
    'twitter.com': 'X (Twitter)', 'x.com': 'X (Twitter)',
    'instagram.com': 'Instagram',
    'facebook.com': 'Facebook', 'fb.watch': 'Facebook',
}


def _cobalt_platform_label(url: str) -> str:
    try:
        host = (urlparse(url).hostname or '').lower()
        if host.startswith('www.'):
            host = host[4:]
        for h, label in _COBALT_PLATFORM_LABELS.items():
            if host == h or host.endswith('.' + h):
                return label
    except Exception:
        pass
    return 'Video'


def _minimal_info_response(label: str):
    """Trả info tối thiểu để người dùng bấm tải được (luồng dán link tay bắt buộc qua
    bước info) khi không đọc nhanh được metadata, thay vì bị chặn từ đầu."""
    return Response({
        'success': True,
        'title': f'Video {label} — không đọc được chi tiết, vẫn có thể thử tải',
        'thumbnail': None,
        'duration': None,
        'uploader': None,
        'extractor': label,
        'best_height': None,
        'filesize_approx': None,
        'limited': True,
    })


def _cobalt_minimal_info_response(url: str):
    """FB/IG/X hay chặn yt-dlp đọc metadata khi không có cookie, nhưng bước tải thật
    đi qua Cobalt nên vẫn có thể thành công — trả info tối thiểu."""
    return _minimal_info_response(_cobalt_platform_label(url))


def _try_cobalt_download(job_id: str, url: str, fmt: str, quality: str, out_base: str) -> bool:
    """Thử tải qua Cobalt. Trả False cho MỌI lỗi (Cobalt chưa chạy, platform chưa hỗ trợ,
    network lỗi...) để _do_download rơi về yt-dlp như cũ — không bao giờ raise ra ngoài."""
    cobalt_url = getattr(settings, 'COBALT_API_URL', 'http://localhost:9000').rstrip('/')
    body = {
        'url': url,
        'downloadMode': 'audio' if fmt == 'mp3' else 'auto',
        'audioFormat': 'mp3' if fmt == 'mp3' else 'best',
    }
    if fmt != 'mp3' and quality != 'best':
        body['videoQuality'] = quality

    try:
        resp = requests.post(
            cobalt_url + '/', json=body,
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
            timeout=COBALT_CONNECT_TIMEOUT,
        )
        data = resp.json()
    except Exception as e:
        logger.info(f"[VideoDL] Cobalt không phản hồi ({e}), rơi về yt-dlp.")
        return False

    if data.get('status') not in ('tunnel', 'redirect'):
        logger.info(f"[VideoDL] Cobalt trả lỗi cho {url[:80]}: {data}. Rơi về yt-dlp.")
        return False

    media_url = data.get('url')
    if not media_url:
        return False

    target = out_base + '.' + fmt
    try:
        progress_update(job_id, {'message': 'Đang tải qua Cobalt...', 'percent': 5})
        with requests.get(media_url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
            r.raise_for_status()
            total = int(r.headers.get('Content-Length') or 0)
            if total and total > COBALT_MAX_FILESIZE_BYTES:
                return False
            received = 0
            last_pct = 0
            with open(target, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    received += len(chunk)
                    if received > COBALT_MAX_FILESIZE_BYTES:
                        raise ValueError('File vượt quá giới hạn kích thước cho phép.')
                    if total:
                        pct = min(99, int(received / total * 100))
                        if pct > last_pct:
                            last_pct = pct
                            progress_update(job_id, {'percent': pct})
        if not os.path.isfile(target) or os.path.getsize(target) < 1024:
            _cleanup_job_files(out_base)
            return False
        if not _media_file_ok(target, fmt):
            logger.warning("[VideoDL] File Cobalt trả về không đọc được bằng ffprobe, rơi về yt-dlp.")
            _cleanup_job_files(out_base)
            return False

        title = os.path.splitext(data.get('filename') or 'video')[0]
        progress_update(job_id, {
            'status': 'done', 'percent': 100, 'message': 'Hoàn tất', 'title': title, 'error': None,
        })
        threading.Timer(UNCLAIMED_FILE_TTL_SECONDS, _expire_unclaimed_file, args=(job_id, out_base)).start()
        return True
    except Exception as e:
        logger.warning(f"[VideoDL] Cobalt tải file thất bại ({e}), rơi về yt-dlp.")
        _cleanup_job_files(out_base)
        return False


# Douyin bắt buộc chữ ký JS (a_bogus/x-secsdk-web-signature) cho MỌI request gọi API chi
# tiết video — yt-dlp tự gọi API đó mà không tính được chữ ký nên luôn báo "Fresh cookies
# (not necessarily logged in) are needed" (xem chính TODO trong code yt-dlp thừa nhận thiếu
# "verification challenge code"). Né bằng cách để CHÍNH trang Douyin tự gọi API đó (tự tính
# đúng chữ ký) trong 1 Chromium headless (Playwright, script riêng — xem lý do chạy subprocess
# thay vì import trực tiếp trong docstring của douyin_extract.py), "nghe trộm" response thành
# công rồi tự đọc JSON lấy link video thật — không qua yt-dlp/Cobalt cho Douyin.
DOUYIN_PLATFORM_HOSTS = ('douyin.com',)
DOUYIN_EXTRACT_TIMEOUT = 60  # bao gồm cả thời gian mở Chromium + tải trang + chờ API ký đúng
DOUYIN_CACHE_TTL_SECONDS = 300  # mở Chromium khá tốn thời gian (~10-20s) — cache để vài lượt
# tải liên tiếp (Kiểm tra rồi Video MP4) không phải mở lại trình duyệt mỗi lần


def _douyin_platform(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or '').lower().lstrip('www.')
        return any(host == h or host.endswith('.' + h) for h in DOUYIN_PLATFORM_HOSTS)
    except Exception:
        return False


def _browser_cache_path(prefix: str, url: str) -> str:
    h = hashlib.md5(url.encode('utf-8')).hexdigest()
    return os.path.join(tempfile.gettempdir(), f'vcb_{prefix}_{h}.json')


def _take_valid_browser_cache(prefix: str, url: str):
    path = _browser_cache_path(prefix, url)
    if not os.path.isfile(path):
        return None
    try:
        if time.time() - os.path.getmtime(path) > DOUYIN_CACHE_TTL_SECONDS:
            os.remove(path)
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def _save_browser_cache(prefix: str, url: str, info: dict):
    try:
        with open(_browser_cache_path(prefix, url), 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False)
    except OSError:
        pass


# Khoá theo URL để 2 luồng (vd "Kiểm tra" đang warm cache + job tải bấm ngay sau đó)
# không mở 2 Chromium cùng lúc cho cùng 1 URL — luồng sau chờ rồi dùng cache của luồng trước.
_extract_locks: dict = {}
_extract_locks_guard = threading.Lock()


def _url_extract_lock(key: str) -> threading.Lock:
    with _extract_locks_guard:
        lock = _extract_locks.get(key)
        if lock is None:
            lock = _extract_locks[key] = threading.Lock()
        return lock


def _script_extract_info(script_name: str, cache_prefix: str, url: str, timeout: int):
    """Chạy script Playwright (process riêng) lấy {title, thumbnail, duration_ms,
    uploader, video_url}, có cache + khoá chống trích xuất trùng. None nếu thất bại."""
    with _url_extract_lock(f'{cache_prefix}:{url}'):
        cached = _take_valid_browser_cache(cache_prefix, url)
        if cached:
            return cached

        script = os.path.join(os.path.dirname(__file__), '..', 'scripts', script_name)
        out_path = os.path.join(tempfile.gettempdir(), f'vcb_{cache_prefix}_out_{uuid.uuid4().hex}.json')
        try:
            proc = subprocess.run(
                [sys.executable, script, url, out_path],
                capture_output=True, text=True, timeout=timeout,
            )
            if proc.returncode != 0 or not os.path.isfile(out_path):
                logger.warning(f"[VideoDL] {cache_prefix} extract thất bại: {(proc.stderr or '')[-400:]}")
                return None
            with open(out_path, 'r', encoding='utf-8') as f:
                info = json.load(f)
            _save_browser_cache(cache_prefix, url, info)
            return info
        except Exception as e:
            logger.warning(f"[VideoDL] {cache_prefix} extract lỗi: {e}")
            return None
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass


def _extract_douyin_info(url: str):
    return _script_extract_info('douyin_extract.py', 'douyin', url, DOUYIN_EXTRACT_TIMEOUT)


def _try_browser_download(job_id: str, url: str, fmt: str, out_base: str,
                          extract_fn, referer: str, label: str) -> bool:
    """Tải trực tiếp link CDN lấy từ trình duyệt ẩn (Douyin). Trả False nếu
    trích xuất/tải thất bại — caller tự quyết định báo lỗi hay rơi về đường khác."""
    info = extract_fn(url)
    if not info or not info.get('video_url'):
        return False

    target = out_base + '.mp4'  # luôn tải video trước, mp3 sẽ tách audio bằng ffmpeg sau
    try:
        progress_update(job_id, {'message': f'Đang tải qua trình duyệt ẩn ({label})...', 'percent': 10})
        headers = {'User-Agent': BROWSER_UA, 'Referer': referer}
        with requests.get(info['video_url'], headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
            r.raise_for_status()
            total = int(r.headers.get('Content-Length') or 0)
            if total and total > COBALT_MAX_FILESIZE_BYTES:
                return False
            received = 0
            last_pct = 0
            with open(target, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    received += len(chunk)
                    if received > COBALT_MAX_FILESIZE_BYTES:
                        raise ValueError('File vượt quá giới hạn kích thước cho phép.')
                    if total:
                        pct = min(89, 10 + int(received / total * 79))
                        if pct > last_pct:
                            last_pct = pct
                            progress_update(job_id, {'percent': pct})
        if not os.path.isfile(target) or os.path.getsize(target) < 1024:
            _cleanup_job_files(out_base)
            return False

        final_target = target
        if fmt == 'mp3':
            ffmpeg = _get_ffmpeg()
            mp3_target = out_base + '.mp3'
            if not ffmpeg or subprocess.run(
                [ffmpeg, '-y', '-i', target, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', mp3_target],
                capture_output=True, timeout=120,
            ).returncode != 0:
                _cleanup_job_files(out_base)
                return False
            os.remove(target)
            final_target = mp3_target

        if not _media_file_ok(final_target, fmt):
            logger.warning(f"[VideoDL] {label} file tải về không đọc được bằng ffprobe, huỷ.")
            _cleanup_job_files(out_base)
            return False

        title = info.get('title') or f'{label.lower()}_video'
        progress_update(job_id, {
            'status': 'done', 'percent': 100, 'message': 'Hoàn tất', 'title': title, 'error': None,
        })
        threading.Timer(UNCLAIMED_FILE_TTL_SECONDS, _expire_unclaimed_file, args=(job_id, out_base)).start()
        return bool(final_target)
    except Exception as e:
        logger.warning(f"[VideoDL] {label} tải file thất bại: {e}")
        _cleanup_job_files(out_base)
        return False


class VideoDownloadThrottle(SimpleRateThrottle):
    """Giới hạn số lần khởi tạo job tải/phút — thao tác nặng nên tách riêng khỏi throttle mặc định."""
    scope = 'video_download'

    def get_cache_key(self, request, view):
        ident = request.user.pk if request.user and request.user.is_authenticated else self.get_ident(request)
        return f'throttle_video_dl_{ident}'


_URL_IN_TEXT_RE = re.compile(r'https?://\S+')


def _extract_url(raw: str) -> str:
    """Nhiều nền tảng (Douyin/TikTok...) khi bấm "Chia sẻ" copy nguyên văn bản kèm link
    (vd "https://v.douyin.com/xxx/ 复制此链接，打开Dou音搜索...") thay vì chỉ link sạch.
    Tách lấy URL đầu tiên trong chuỗi thay vì chỉ strip() khoảng trắng 2 đầu."""
    raw = (raw or '').strip()
    m = _URL_IN_TEXT_RE.search(raw)
    url = m.group(0).rstrip('，,。.!！?？、') if m else raw
    return _normalize_douyin_modal_url(url)


_DOUYIN_MODAL_ID_RE = re.compile(r'^https?://(?:www\.)?douyin\.com/[^?#]*\?.*\bmodal_id=(\d+)', re.I)


def _normalize_douyin_modal_url(url: str) -> str:
    """Trang "Đề xuất" (jingxuan/discover...) của Douyin hiện video trong modal, dùng
    query param modal_id thay vì đường dẫn /video/<id> — yt-dlp không nhận diện được
    dạng này (báo "Unsupported URL"). Quy đổi về /video/<id> để nhận diện đúng làm 1
    video (dù bản chất Douyin vẫn cần cookie sinh bằng JS, quy đổi này không giải quyết
    được giới hạn đó — chỉ giúp báo đúng lỗi thay vì lỗi không liên quan)."""
    m = _DOUYIN_MODAL_ID_RE.match(url)
    return f'https://www.douyin.com/video/{m.group(1)}' if m else url


def _validate_url(url: str) -> bool:
    return bool(url) and bool(re.match(r'^https?://', url.strip()))


# Link trang cá nhân/kênh (không phải 1 video cụ thể) — nếu đưa thẳng cho yt-dlp, nó sẽ cố
# liệt kê TOÀN BỘ video của trang đó (--no-playlist không chặn được việc này, chỉ chặn
# playlist thường) — có thể treo hàng chục giây tới khi hết INFO_TIMEOUT/DOWNLOAD_TIMEOUT.
# Xảy ra thực tế khi extension bắt nhầm link trang thay vì link video (vd rê chuột vào
# thumbnail trên trang lưới cá nhân TikTok mà không tìm thấy đúng link video/permalink).
_PROFILE_URL_PATTERNS = [
    re.compile(r'^https?://(www\.)?tiktok\.com/@[^/?#]+/?$', re.I),
    re.compile(r'^https?://(www\.)?(twitter|x)\.com/[^/?#]+/?$', re.I),
    re.compile(r'^https?://(www\.)?instagram\.com/[^/?#]+/?$', re.I),
    re.compile(r'^https?://(www\.)?facebook\.com/[^/?#]+/?$', re.I),
    re.compile(r'^https?://(www\.)?douyin\.com/user/[^/?#]+/?$', re.I),
    re.compile(r'^https?://(www\.)?youtube\.com/(@[^/?#]+|channel/[^/?#]+|c/[^/?#]+)/?$', re.I),
]


def _looks_like_profile_url(url: str) -> bool:
    return any(p.match(url.strip()) for p in _PROFILE_URL_PATTERNS)


def _is_public_url(url: str) -> bool:
    """
    Chặn SSRF: từ chối URL trỏ vào IP nội bộ/loopback/link-local/metadata.
    Lưu ý: chỉ kiểm tra IP resolve ban đầu — không pin IP cho các redirect mà
    yt-dlp tự theo dõi bên trong, nên đây là lớp chặn chính chứ không tuyệt đối
    trước tấn công DNS-rebinding có chủ đích.
    """
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return False
        return True
    except Exception:
        logger.warning(f"[VideoDL] Could not resolve host for SSRF check: {url[:100]}")
        return False


class _SelfDeletingFile:
    """File wrapper tự xoá file tạm khi FileResponse stream xong."""

    def __init__(self, path: str):
        self.path = path
        self._f = open(path, 'rb')

    def read(self, *args, **kwargs):
        return self._f.read(*args, **kwargs)

    def close(self):
        try:
            self._f.close()
        finally:
            try:
                os.remove(self.path)
            except OSError:
                pass


def _safe_filename(title: str, ext: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', '', title or 'video').strip() or 'video'
    return f"{name[:80]}.{ext}"


def ytdlp_ffmpeg_args(ffmpeg: str) -> list:
    """Cờ `--ffmpeg-location` cho yt-dlp — trỏ THẲNG vào binary, không phải thư mục chứa nó.

    `--ffmpeg-location` nhận cả hai dạng, nhưng đưa thư mục thì yt-dlp đi tìm một file tên đúng
    `ffmpeg` bên trong. Bản `imageio_ffmpeg` đang dùng đặt tên theo nền tảng
    (`ffmpeg-macos-x86_64-v7.1`) nên trong thư mục đó KHÔNG hề có file tên `ffmpeg` — yt-dlp coi
    như không có ffmpeg, bỏ luôn bước ghép hình với tiếng mà vẫn thoát mã 0.
    """
    return ['--ffmpeg-location', ffmpeg] if ffmpeg else []


def _get_ffprobe() -> str:
    """ffprobe nằm cùng thư mục với ffmpeg."""
    ffmpeg = _get_ffmpeg()
    if not ffmpeg:
        return ''
    d = os.path.dirname(ffmpeg)
    for name in ('ffprobe.exe', 'ffprobe'):
        cand = os.path.join(d, name)
        if os.path.isfile(cand):
            return cand
    import shutil
    return shutil.which('ffprobe') or ''


def _media_file_ok(path: str, fmt: str) -> bool:
    """Xác nhận file tải về THẬT SỰ mở được: container đọc được và có đúng loại stream
    (video cho mp4, audio cho mp3). Chặn trường hợp "tải được nhưng không xem được" —
    vd trang lỗi HTML bị lưu thành .mp4, hay file ghép dở. Thiếu ffprobe thì bỏ qua
    (đành tin file) thay vì chặn nhầm."""
    ffprobe = _get_ffprobe()
    if not ffprobe:
        return True
    try:
        proc = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', path],
            capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            return False
        streams = (json.loads(proc.stdout.decode('utf-8', 'ignore')) or {}).get('streams') or []
        kinds = {s.get('codec_type') for s in streams}
        return ('video' in kinds) if fmt == 'mp4' else ('audio' in kinds)
    except Exception as e:
        logger.warning(f"[VideoDL] ffprobe kiểm tra file lỗi ({e}) — coi như file hợp lệ.")
        return True


# Link chia sẻ Facebook (Copy link trên app/web ra dạng /share/..., fb.watch, l.facebook.com)
# chỉ là redirect — Cobalt báo "link.unsupported" còn yt-dlp cũng hay trượt. Giải về URL
# video thật (facebook.com/watch?v=... hoặc /reel/...) trước khi xử lý.
_FB_SHARE_URL_RE = re.compile(
    r'^https?://((www\.)?facebook\.com/(share|story\.php)|fb\.watch/|l\.facebook\.com/)', re.I)
_FB_LOGIN_NEXT_RE = re.compile(r'[?&]next=([^&]+)')


def _resolve_share_url(url: str) -> str:
    if not _FB_SHARE_URL_RE.match(url):
        return url
    try:
        r = requests.get(
            url, headers={'User-Agent': BROWSER_UA}, allow_redirects=True,
            timeout=10, stream=True,
        )
        final = r.url
        r.close()
        # Bị đá sang trang đăng nhập: URL video thật nằm trong query ?next=
        if final and '/login' in final:
            m = _FB_LOGIN_NEXT_RE.search(final)
            if m:
                from urllib.parse import unquote
                final = unquote(m.group(1))
        if final and final != url and _validate_url(final):
            logger.info(f"[VideoDL] Giải link chia sẻ FB: {url[:80]} → {final[:80]}")
            return final
    except Exception as e:
        logger.info(f"[VideoDL] Không giải được link chia sẻ FB ({e}), dùng nguyên URL gốc.")
    return url


def _ytdlp_error_tail(stderr_bytes) -> str:
    """Đuôi stderr của yt-dlp nhưng bỏ dòng cảnh báo không liên quan (vd "Deprecated
    Feature: Python 3.10") — trước đây warning này chiếm trọn 400 ký tự cuối làm log
    không bao giờ hiện lỗi thật."""
    text = (stderr_bytes or b'').decode('utf-8', 'ignore')
    lines = [l for l in text.splitlines() if l.strip() and 'Deprecated Feature' not in l]
    return ' | '.join(lines)[-400:]


def _info_cache_path(url: str) -> str:
    h = hashlib.md5(url.encode('utf-8')).hexdigest()
    return os.path.join(tempfile.gettempdir(), f'vcb_infocache_{h}.json')


def _save_info_cache(url: str, raw_json: str):
    try:
        with open(_info_cache_path(url), 'w', encoding='utf-8') as f:
            f.write(raw_json)
    except OSError:
        pass


def _take_valid_info_cache(url: str):
    """Trả về path file cache hợp lệ (còn hạn + JSON không hỏng) để dùng với
    --load-info-json, hoặc None nếu không có/hết hạn/hỏng (dọn luôn nếu vậy)."""
    path = _info_cache_path(url)
    if not os.path.isfile(path):
        return None
    try:
        if time.time() - os.path.getmtime(path) > INFO_CACHE_TTL_SECONDS:
            os.remove(path)
            return None
        with open(path, 'r', encoding='utf-8') as f:
            json.loads(f.read())  # chỉ để xác nhận file không bị ghi dở/hỏng
        return path
    except (OSError, ValueError):
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def finished_media_path(out_base: str, fmt: str):
    """Đường dẫn file ĐÃ GHÉP XONG của một job, hoặc None nếu chưa có.

    Dùng chung cho cả lúc kết thúc job lẫn lúc trả file về — trước đây mỗi bên tự suy ra một
    kiểu (`endswith('.mp4')` bên này, ghép chuỗi đúng nghĩa đen bên kia) nên có lúc bên này bảo
    xong mà bên kia không tìm thấy file, trả 410 ngay sau khi báo "Hoàn tất".

    File hoàn chỉnh LUÔN là đúng `<out_base>.<fmt>`: yt-dlp ghép ra tên đó rồi tự xoá mảnh. Mọi
    tên khác đều là mảnh dở (`<out_base>.f395.mp4` chỉ có hình, `.f251.webm` chỉ có tiếng) —
    chúng cũng kết thúc bằng đuôi đã yêu cầu nên KHÔNG được lọc bằng đuôi.
    """
    path = out_base + '.' + fmt
    return path if os.path.isfile(path) else None


def _cleanup_job_files(out_base: str):
    for f in glob.glob(out_base + '.*'):
        try:
            os.remove(f)
        except OSError:
            pass


def _expire_unclaimed_file(job_id: str, out_base: str):
    """Dọn file nếu sau UNCLAIMED_FILE_TTL_SECONDS mà chưa ai tải về (job vẫn ở status='done')."""
    data = progress_get(job_id)
    if data is not None and data.get('status') == 'delivered':
        return  # đã tải về rồi, _SelfDeletingFile tự lo
    removed = glob.glob(out_base + '.*')
    _cleanup_job_files(out_base)
    if removed:
        logger.info(f"[VideoDL] Job {job_id} hết hạn {UNCLAIMED_FILE_TTL_SECONDS}s chưa ai tải, đã dọn file.")


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def video_info(request):
    """Lấy metadata video (title, thumbnail, duration, uploader, độ phân giải cao nhất) qua yt-dlp -J."""
    url = _extract_url(str(request.data.get('url', '')))
    if not _validate_url(url):
        return Response({'success': False, 'error': 'URL không hợp lệ.'}, status=400)
    url = _resolve_share_url(url)
    if not _is_public_url(url):
        return Response({'success': False, 'error': 'URL không được hỗ trợ.'}, status=400)
    if _looks_like_profile_url(url):
        return Response({'success': False, 'error': 'Đây là link trang cá nhân/kênh, không phải link 1 video cụ thể. Mở đúng video rồi copy link của video đó.'}, status=400)

    if _douyin_platform(url):
        # yt-dlp không đọc được Douyin (thiếu chữ ký JS) — dùng trình duyệt ẩn thay thế.
        info = _extract_douyin_info(url)
        if not info:
            return Response({'success': False, 'error': 'Không đọc được thông tin video Douyin. Video có thể riêng tư hoặc đã bị xoá.'}, status=400)
        return Response({
            'success': True,
            'title': info.get('title'),
            'thumbnail': info.get('thumbnail'),
            'duration': (info.get('duration_ms') or 0) / 1000 or None,
            'uploader': info.get('uploader'),
            'extractor': 'Douyin',
            'best_height': None,
            'filesize_approx': None,
        })

    ytdlp = _get_ytdlp()
    if not ytdlp:
        return Response({'success': False, 'error': 'Hệ thống thiếu yt-dlp.'}, status=500)

    cmd = [ytdlp, '-J', '--no-playlist', '--no-warnings', '--no-check-certificates',
           '--socket-timeout', '20', *COMMON_HEADERS, url]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=INFO_TIMEOUT)
        if proc.returncode != 0:
            err = _ytdlp_error_tail(proc.stderr)
            logger.warning(f"[VideoDL] info failed: {err}")
            if _cobalt_platform(url):
                return _cobalt_minimal_info_response(url)
            return Response({'success': False, 'error': 'Không đọc được thông tin video. Kiểm tra lại link hoặc quyền truy cập.'}, status=400)
        raw_stdout = proc.stdout.decode('utf-8', 'ignore')
        info = json.loads(raw_stdout)
        _save_info_cache(url, raw_stdout)

        formats = info.get('formats') or []
        heights = [f.get('height') for f in formats if f.get('height')]
        best_height = max(heights) if heights else None

        filesize = info.get('filesize') or info.get('filesize_approx')
        if not filesize:
            candidates = [f for f in formats if f.get('filesize') or f.get('filesize_approx')]
            if candidates:
                best = max(candidates, key=lambda f: f.get('height') or 0)
                filesize = best.get('filesize') or best.get('filesize_approx')

        return Response({
            'success': True,
            'title': info.get('title'),
            'thumbnail': info.get('thumbnail'),
            'duration': info.get('duration'),
            'uploader': info.get('uploader') or info.get('channel'),
            'extractor': info.get('extractor_key'),
            'best_height': best_height,
            'filesize_approx': filesize,
        })
    except subprocess.TimeoutExpired:
        if _cobalt_platform(url):
            return _cobalt_minimal_info_response(url)
        return Response({'success': False, 'error': 'Hết thời gian đọc thông tin video.'}, status=504)
    except Exception as e:
        logger.error(f"[VideoDL] info error: {e}", exc_info=True)
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([VideoDownloadThrottle])
def start_download(request):
    """
    Khởi tạo job tải chạy nền, trả job_id ngay (202) để tránh timeout của Cloudflare Tunnel.
    Body: {url, type: mp4|mp3, quality: best|1080|720}
    """
    url = _extract_url(str(request.data.get('url', '')))
    fmt = str(request.data.get('type', 'mp4')).strip().lower()
    quality = str(request.data.get('quality', 'best')).strip().lower()
    if fmt not in ('mp4', 'mp3'):
        fmt = 'mp4'
    if quality not in ('best', '1080', '720'):
        quality = 'best'
    if not _validate_url(url):
        return Response({'success': False, 'error': 'URL không hợp lệ.'}, status=400)
    url = _resolve_share_url(url)
    if not _is_public_url(url):
        return Response({'success': False, 'error': 'URL không được hỗ trợ.'}, status=400)
    if _looks_like_profile_url(url):
        return Response({'success': False, 'error': 'Đây là link trang cá nhân/kênh, không phải link 1 video cụ thể. Mở đúng video rồi copy link của video đó.'}, status=400)

    ytdlp = _get_ytdlp()
    if not ytdlp:
        return Response({'success': False, 'error': 'Hệ thống thiếu yt-dlp.'}, status=500)

    job_id = uuid.uuid4().hex
    progress_set(job_id, {
        'status': 'queued',
        'percent': 0,
        'message': 'Đang xếp hàng...',
        'error': None,
        'format': fmt,
        'title': None,
    })

    active = _DL_MAX_CONCURRENT - _dl_semaphore._value
    if active >= _DL_MAX_CONCURRENT:
        logger.info(f"[VideoDL] Queue full ({active}/{_DL_MAX_CONCURRENT}). Job {job_id} sẽ chờ.")

    t = threading.Thread(
        target=_run_download_job,
        args=(job_id, url, fmt, quality),
        daemon=False,
        name=f"video-dl-{job_id[:8]}",
    )
    t.start()

    return Response({'success': True, 'job_id': job_id}, status=202)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_status(request, job_id: str):
    if not JOB_ID_RE.match(job_id or ''):
        return Response({'error': 'job_id không hợp lệ.'}, status=400)
    data = progress_get(job_id)
    if data is None:
        return Response({'error': 'Không tìm thấy job. Server có thể vừa khởi động lại.'}, status=404)
    return Response(data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_file(request, job_id: str):
    """Stream file đã tải xong về trình duyệt. Chỉ dùng được khi status='done'."""
    if not JOB_ID_RE.match(job_id or ''):
        return Response({'error': 'job_id không hợp lệ.'}, status=400)
    data = progress_get(job_id)
    if data is None:
        return Response({'error': 'Không tìm thấy job.'}, status=404)
    if data.get('status') != 'done':
        return Response({'error': 'File chưa sẵn sàng.'}, status=409)

    fmt = data.get('format', 'mp4')
    out_base = os.path.join(tempfile.gettempdir(), f'vcb_dl_{job_id}')
    # Cùng một hàm với lúc kết thúc job — job đã báo 'done' thì tới đây chắc chắn phải thấy file.
    target = finished_media_path(out_base, fmt)
    if not target:
        return Response({'error': 'File không còn tồn tại (có thể đã hết hạn).'}, status=410)

    filename = _safe_filename(data.get('title') or 'video', fmt)
    size = os.path.getsize(target)
    resp = FileResponse(_SelfDeletingFile(target), as_attachment=True, filename=filename)
    resp['Content-Length'] = size
    progress_update(job_id, {'status': 'delivered'})
    return resp


def _run_download_job(job_id: str, url: str, fmt: str, quality: str):
    progress_update(job_id, {'message': 'Đang chờ tới lượt xử lý...'})
    with _dl_semaphore:
        _do_download(job_id, url, fmt, quality)


def _do_download(job_id: str, url: str, fmt: str, quality: str):
    out_base = os.path.join(tempfile.gettempdir(), f'vcb_dl_{job_id}')

    if _cobalt_platform(url) and _try_cobalt_download(job_id, url, fmt, quality, out_base):
        return

    if _douyin_platform(url):
        # Không rơi về yt-dlp — đã xác nhận yt-dlp luôn thất bại với Douyin (thiếu chữ ký
        # JS), chạy tiếp chỉ tốn thời gian chờ vô ích cho cùng 1 kết quả lỗi.
        if not _try_browser_download(job_id, url, fmt, out_base, _extract_douyin_info,
                                     'https://www.douyin.com/', 'Douyin'):
            _cleanup_job_files(out_base)
            progress_update(job_id, {
                'status': 'error', 'percent': 0, 'message': '',
                'error': 'Tải video Douyin thất bại. Video có thể riêng tư, đã bị xoá, hoặc trình duyệt ẩn gặp sự cố.',
            })
        return

    ffmpeg = _get_ffmpeg()
    ytdlp = _get_ytdlp()

    cmd = [ytdlp, '--no-playlist', '--no-warnings', '--no-check-certificates', '--no-color',
           '--newline', '--socket-timeout', '30', *COMMON_HEADERS,
           '--concurrent-fragments', '16',
           # TikTok/Facebook/Instagram trả về 1 file HTTP thường (không fragment DASH/HLS),
           # nên --concurrent-fragments một mình không có tác dụng với chúng.
           # --http-chunk-size buộc yt-dlp chia file đó thành nhiều chunk tải song song
           # qua HTTP Range request — tăng tốc thật cho các link dạng này mà không đổi
           # format/chất lượng đã chọn. Để 1M (không phải 10M) vì clip TikTok/Reels/Shorts
           # ngắn thường chỉ vài MB — ngưỡng 10M sẽ khiến các video đó KHÔNG được chia nhỏ
           # chút nào (không có tác dụng đúng lúc cần nhất).
           '--http-chunk-size', '1M',
           '--max-filesize', MAX_FILESIZE,
           '--output', out_base + '.%(ext)s',
           '--print', 'after_move:title']
    cmd += ytdlp_ffmpeg_args(ffmpeg)
    if fmt == 'mp3':
        cmd += ['--format', 'bestaudio/best', '--extract-audio', '--audio-format', 'mp3', '--audio-quality', '0']
    else:
        if quality == 'best':
            vfmt = 'bestvideo+bestaudio/best'
        else:
            vfmt = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'
        # Cùng độ phân giải thì ưu tiên H.264/AAC — VP9/AV1 ghép vào .mp4 vẫn hợp lệ về
        # container nhưng Windows Media Player/mobile mặc định KHÔNG phát được ("tải
        # được mà không xem được"). Độ phân giải vẫn xếp trước nên không giảm chất lượng.
        cmd += ['--format', vfmt, '--merge-output-format', 'mp4',
                '--format-sort', 'res,fps,vcodec:h264,acodec:m4a']

    # Nếu người dùng vừa bấm "Kiểm tra" (video_info) trước đó không lâu, dùng lại luôn
    # kết quả trích xuất đó thay vì bắt yt-dlp trích xuất lại từ đầu — đỡ hẳn bước gọi
    # trang/API của nền tảng (thường là phần chiếm nhiều giây nhất trước khi có % tải thật).
    info_cache = _take_valid_info_cache(url)
    if info_cache:
        cmd += ['--load-info-json', info_cache]
    else:
        cmd.append(url)

    progress_update(job_id, {'status': 'downloading', 'message': 'Đang tải...', 'percent': 1})
    title_lines = []
    tail_lines = []  # giữ đuôi output để log được lỗi thật khi yt-dlp thất bại
    last_pct = 0
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding='utf-8', errors='ignore', bufsize=1)

        def _watchdog():
            try:
                proc.wait(timeout=DOWNLOAD_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()

        wd = threading.Thread(target=_watchdog, daemon=True)
        wd.start()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if 'Deprecated Feature' not in line and not line.startswith('[download]'):
                tail_lines.append(line)
                if len(tail_lines) > 8:
                    tail_lines.pop(0)
            m = _PERCENT_RE.search(line)
            if m:
                # Dành 10% cuối cho bước ghép audio/video nên co về thang 0-90
                raw_pct = min(90, int(float(m.group(1)) * 0.9))
                if raw_pct > last_pct:
                    last_pct = raw_pct
                    progress_update(job_id, {'percent': last_pct})
            elif '[Merger]' in line or '[ExtractAudio]' in line or '[ffmpeg]' in line:
                progress_update(job_id, {'percent': 95, 'message': 'Đang ghép âm thanh & video...'})
            elif line and not line.startswith('['):
                title_lines.append(line)

        proc.stdout.close()
        returncode = proc.wait()
        wd.join(timeout=1)

        files = glob.glob(out_base + '.*')
        # Phải là ĐÚNG file đã ghép xong, không phải "file nào kết thúc bằng đuôi này". Mảnh tạm
        # của yt-dlp (`<out_base>.f395.mp4`) cũng kết thúc bằng `.mp4` nên lọc theo đuôi vẫn lọt:
        # bản cũ nhận nguyên mảnh chỉ-có-hình làm video hoàn chỉnh rồi báo "Hoàn tất".
        # Dùng chung hàm với `download_file` để hai bên không thể hiểu khác nhau về "file xong".
        target = finished_media_path(out_base, fmt)
        if returncode != 0 or not target:
            logger.warning(
                f"[VideoDL] job {job_id} yt-dlp thất bại (rc={returncode}, url={url[:80]}): "
                + ' | '.join(tail_lines)[-500:]
            )
            _cleanup_job_files(out_base)
            progress_update(job_id, {
                'status': 'error', 'percent': 0, 'message': '',
                'error': 'Tải video thất bại. Video có thể bị riêng tư hoặc nền tảng chặn.',
            })
            return

        for other in files:
            if other != target:
                try:
                    os.remove(other)
                except OSError:
                    pass

        if not _media_file_ok(target, fmt):
            logger.warning(f"[VideoDL] job {job_id}: file yt-dlp trả về không đọc được bằng ffprobe.")
            _cleanup_job_files(out_base)
            progress_update(job_id, {
                'status': 'error', 'percent': 0, 'message': '',
                'error': 'File tải về bị lỗi (không phát được). Thử lại với chất lượng thấp hơn hoặc link khác.',
            })
            return

        title = title_lines[-1] if title_lines else 'video'
        progress_update(job_id, {
            'status': 'done', 'percent': 100, 'message': 'Hoàn tất', 'title': title, 'error': None,
        })
        threading.Timer(UNCLAIMED_FILE_TTL_SECONDS, _expire_unclaimed_file, args=(job_id, out_base)).start()
    except Exception as e:
        logger.error(f"[VideoDL] job {job_id} error: {e}", exc_info=True)
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        _cleanup_job_files(out_base)
        progress_update(job_id, {'status': 'error', 'error': f'Lỗi không xác định: {e}'})
    finally:
        if info_cache:
            try:
                os.remove(info_cache)
            except OSError:
                pass
