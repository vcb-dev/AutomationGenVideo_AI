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
import json
import uuid
import glob
import socket
import logging
import ipaddress
import tempfile
import threading
import subprocess
from urllib.parse import urlparse

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

_DL_MAX_CONCURRENT = 3
_dl_semaphore = threading.Semaphore(_DL_MAX_CONCURRENT)

# Nếu job tải xong nhưng người dùng không bao giờ bấm tải về (đóng tab, mất mạng...),
# _SelfDeletingFile không bao giờ được gọi tới → file nằm mãi trên disk. Dọn cưỡng bức sau 30 phút.
UNCLAIMED_FILE_TTL_SECONDS = 1800

JOB_ID_RE = re.compile(r'^[0-9a-f]{32}$')
_PERCENT_RE = re.compile(r'\[download\]\s+([\d.]+)%')

COMMON_HEADERS = [
    '--add-header', 'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    '--add-header', 'Accept-Language:vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
]


class VideoDownloadThrottle(SimpleRateThrottle):
    """Giới hạn số lần khởi tạo job tải/phút — thao tác nặng nên tách riêng khỏi throttle mặc định."""
    scope = 'video_download'

    def get_cache_key(self, request, view):
        ident = request.user.pk if request.user and request.user.is_authenticated else self.get_ident(request)
        return f'throttle_video_dl_{ident}'


def _validate_url(url: str) -> bool:
    return bool(url) and bool(re.match(r'^https?://', url.strip()))


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
    url = str(request.data.get('url', '')).strip()
    if not _validate_url(url):
        return Response({'success': False, 'error': 'URL không hợp lệ.'}, status=400)
    if not _is_public_url(url):
        return Response({'success': False, 'error': 'URL không được hỗ trợ.'}, status=400)

    ytdlp = _get_ytdlp()
    if not ytdlp:
        return Response({'success': False, 'error': 'Hệ thống thiếu yt-dlp.'}, status=500)

    cmd = [ytdlp, '-J', '--no-playlist', '--no-warnings', '--no-check-certificates',
           '--socket-timeout', '20', *COMMON_HEADERS, url]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=INFO_TIMEOUT)
        if proc.returncode != 0:
            err = (proc.stderr or b'').decode('utf-8', 'ignore')[-400:]
            logger.warning(f"[VideoDL] info failed: {err}")
            return Response({'success': False, 'error': 'Không đọc được thông tin video. Kiểm tra lại link hoặc quyền truy cập.'}, status=400)
        info = json.loads(proc.stdout.decode('utf-8', 'ignore'))

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
    url = str(request.data.get('url', '')).strip()
    fmt = str(request.data.get('type', 'mp4')).strip().lower()
    quality = str(request.data.get('quality', 'best')).strip().lower()
    if fmt not in ('mp4', 'mp3'):
        fmt = 'mp4'
    if quality not in ('best', '1080', '720'):
        quality = 'best'
    if not _validate_url(url):
        return Response({'success': False, 'error': 'URL không hợp lệ.'}, status=400)
    if not _is_public_url(url):
        return Response({'success': False, 'error': 'URL không được hỗ trợ.'}, status=400)

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
    target = out_base + '.' + fmt
    if not os.path.isfile(target):
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
    ffmpeg = _get_ffmpeg()
    ytdlp = _get_ytdlp()
    out_base = os.path.join(tempfile.gettempdir(), f'vcb_dl_{job_id}')

    cmd = [ytdlp, '--no-playlist', '--no-warnings', '--no-check-certificates', '--no-color',
           '--newline', '--socket-timeout', '30', *COMMON_HEADERS,
           '--concurrent-fragments', '8',
           '--max-filesize', MAX_FILESIZE,
           '--output', out_base + '.%(ext)s',
           '--print', 'after_move:title']
    if ffmpeg:
        cmd += ['--ffmpeg-location', os.path.dirname(ffmpeg) if os.path.isabs(ffmpeg) else ffmpeg]
    if fmt == 'mp3':
        cmd += ['--format', 'bestaudio/best', '--extract-audio', '--audio-format', 'mp3', '--audio-quality', '0']
    else:
        if quality == 'best':
            vfmt = 'bestvideo+bestaudio/best'
        else:
            vfmt = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'
        cmd += ['--format', vfmt, '--merge-output-format', 'mp4']
    cmd.append(url)

    progress_update(job_id, {'status': 'downloading', 'message': 'Đang tải...', 'percent': 1})
    title_lines = []
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
        if returncode != 0 and not files:
            _cleanup_job_files(out_base)
            progress_update(job_id, {
                'status': 'error', 'percent': 0, 'message': '',
                'error': 'Tải video thất bại. Video có thể bị riêng tư hoặc nền tảng chặn.',
            })
            return

        target = next((f for f in files if f.lower().endswith('.' + fmt)), files[0] if files else None)
        if not target or not os.path.isfile(target):
            _cleanup_job_files(out_base)
            progress_update(job_id, {'status': 'error', 'error': 'Không tìm thấy file sau khi tải.'})
            return

        for other in files:
            if other != target:
                try:
                    os.remove(other)
                except OSError:
                    pass

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
