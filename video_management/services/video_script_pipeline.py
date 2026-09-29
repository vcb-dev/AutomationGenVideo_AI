"""Video → kịch bản: tải video về để phân tích, rồi đưa cho engine viết kịch bản.

Dùng cho Bộ Sưu Tập: leader duyệt đề xuất (hoặc thêm thẳng) một video → BE gọi
/api/scraped-video/script-from-video/ chạy nền → kịch bản tiếng Việt viết lại từ lời thoại
và hình ảnh của chính video đó, thay vì chỉ từ tiêu đề + mô tả như trước.

── Tải video: miễn phí trước, TikHub dự phòng ────────────────────────────────
1. Trình tải miễn phí của công cụ "Tải video" (video_downloader_views._do_download) — Reddit
   cũng đi đường này (yt-dlp có trình trích xuất Reddit):
   yt-dlp / Cobalt / trình duyệt ẩn cho Douyin. Đo trên image Railway: TikTok, YouTube,
   Facebook, Instagram, Bilibili tải được; Douyin chập chờn vì chống bot ("ArgusSecurityPlugin
   Sign Invalid", trang thử thách khi bị gọi nhiều); Xiaohongshu và Kuaishou không tải được.
2. Hỏng thì xin nguồn tải MỚI qua TikHub ngay lúc đó (tikhub_download_source) rồi tải thẳng
   file mp4, hoặc dùng ffmpeg với luồng HLS / luồng hình + tiếng tách rời (Bilibili, Reddit). Link TikHub có hạn (~3 giờ với Douyin) nên KHÔNG dùng lại link lấy lúc đề
   xuất — đề xuất có thể nằm chờ duyệt cả ngày. Tốn phí chỉ ở những lượt miễn phí thất bại.

── Engine viết kịch bản ──────────────────────────────────────────────────────
Gemini (gemini_video_script.py, model VIDEO_SCRIPT_GEMINI_MODEL — mặc định gemini-3.1-flash-lite) —
tự bật khi có GEMINI_API_KEY (khoá chung; model riêng, không đọc GEMINI_MODEL của ảnh thẻ).
VIDEO_SCRIPT_ENGINE=off để tắt:
khi đó endpoint vẫn tải video rồi trả status ENGINE_DISABLED để BE giữ cách viết kịch bản cũ.
"""

import glob
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Chất lượng tải để phân tích: đủ cho Gemini nhìn hình, file nhẹ (1-10MB với clip ngắn) để
# upload nhanh. Video dài hơn MAX_SECONDS bị cắt lấy phần đầu — đủ để viết kịch bản, và giữ
# chi phí Gemini (tính theo độ dài) không vọt lên vì một video dài bất thường.
ANALYSIS_QUALITY = '480'
DEFAULT_MAX_SECONDS = 300
# Nền tảng có đường dự phòng TikHub (xem tikhub_download_source.py). Không có: Facebook (TikHub không
# hỗ trợ), YouTube (link TikHub gắn cứng IP máy chủ của họ → luôn 403 khi tải từ máy mình).
TIKHUB_FALLBACK_PLATFORMS = {'douyin', 'tiktok', 'kuaishou', 'xiaohongshu', 'instagram', 'bilibili', 'reddit'}
MAX_DIRECT_DOWNLOAD_BYTES = 300 * 1024 * 1024
DIRECT_DOWNLOAD_TIMEOUT = 60      # giây cho TỪNG lần đọc (requests không có hạn cho cả lượt)
# Hạn cho CẢ lượt tải thẳng. Đo trên image Railway: link TikHub của Kuaishou là bản gốc
# >100MB cho clip 85s, CDN Trung Quốc nhả ~300KB/s → một lượt kéo 5-10 phút. Hết hạn thì
# dừng và cứu phần đầu đã tải (đủ để viết kịch bản) thay vì chờ vô hạn.
DIRECT_DOWNLOAD_DEADLINE = 180

_PLATFORM_HOSTS = [
    ('douyin', ('douyin.com', 'iesdouyin.com')),
    ('tiktok', ('tiktok.com',)),
    ('xiaohongshu', ('xiaohongshu.com', 'xhslink.com', 'rednote.com')),
    ('kuaishou', ('kuaishou.com',)),
    ('bilibili', ('bilibili.com', 'b23.tv')),
    ('youtube', ('youtube.com', 'youtu.be')),
    ('facebook', ('facebook.com', 'fb.watch')),
    ('instagram', ('instagram.com',)),
    ('reddit', ('reddit.com', 'redd.it')),
]


class VideoDownloadError(Exception):
    """Không tải được video bằng mọi đường. `reasons` liệt kê lý do từng đường để BE/FE báo rõ."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__('; '.join(self.reasons) or 'Không tải được video.')


@dataclass
class DownloadedVideo:
    path: str
    duration: float
    has_audio: bool
    size_bytes: int
    source: str          # 'free' | 'tikhub'
    trimmed: bool = False

    def as_dict(self) -> dict:
        return {
            'source': self.source,
            'duration': round(self.duration, 1),
            'has_audio': self.has_audio,
            'size_mb': round(self.size_bytes / 1e6, 2),
            'trimmed': self.trimmed,
        }


def max_seconds() -> int:
    try:
        return max(30, int(getattr(settings, 'VIDEO_SCRIPT_MAX_SECONDS', 0) or os.getenv('VIDEO_SCRIPT_MAX_SECONDS', DEFAULT_MAX_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SECONDS


def detect_platform(url: str) -> str:
    host = ''
    m = re.match(r'^https?://([^/?#]+)', (url or '').strip(), re.I)
    if m:
        host = m.group(1).lower()
    for platform, domains in _PLATFORM_HOSTS:
        if any(host == d or host.endswith('.' + d) for d in domains):
            return platform
    return ''


def _probe(path: str) -> tuple:
    """(độ dài giây, có luồng âm thanh, có luồng hình). ffprobe hỏng thì coi như file hỏng."""
    from video_management.views import video_downloader_views as dl
    ffprobe = dl._get_ffprobe() or 'ffprobe'
    proc = subprocess.run(
        [ffprobe, '-v', 'error', '-show_entries', 'format=duration:stream=codec_type', '-of', 'json', path],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return 0.0, False, False
    info = json.loads(proc.stdout or '{}')
    kinds = {s.get('codec_type') for s in info.get('streams') or []}
    try:
        duration = float((info.get('format') or {}).get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return duration, 'audio' in kinds, 'video' in kinds


def _trim(path: str, seconds: int) -> Optional[str]:
    """Cắt lấy `seconds` giây đầu (copy luồng, không mã hoá lại). Trả đường dẫn file mới."""
    from video_management.views import video_downloader_views as dl
    ffmpeg = dl._get_ffmpeg() or 'ffmpeg'
    out = os.path.splitext(path)[0] + '.trim.mp4'
    proc = subprocess.run(
        [ffmpeg, '-y', '-v', 'error', '-i', path, '-t', str(seconds), '-c', 'copy', out],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not os.path.isfile(out):
        logger.warning(f'[VideoScript] cắt video lỗi: {proc.stderr[-300:]}')
        return None
    return out


def _describe(path: str, source: str) -> DownloadedVideo:
    duration, has_audio, has_video = _probe(path)
    if not has_video:
        raise ValueError('file tải về không có luồng hình')
    trimmed = False
    limit = max_seconds()
    if duration > limit:
        cut = _trim(path, limit)
        if cut:
            try:
                os.remove(path)
            except OSError:
                pass
            path, trimmed = cut, True
            duration, has_audio, _ = _probe(path)
    return DownloadedVideo(path, duration, has_audio, os.path.getsize(path), source, trimmed)


def _download_free(video_url: str) -> tuple:
    """Tải bằng trình tải miễn phí có sẵn. Trả (đường dẫn file | None, lý do lỗi)."""
    from video_management.views import video_downloader_views as dl
    from video_management.views.mix_progress_store import progress_set, progress_get

    job_id = f'vs{uuid.uuid4().hex[:12]}'
    progress_set(job_id, {'status': 'queued', 'percent': 0})
    # Chung giới hạn số lượt tải đồng thời với công cụ Tải video để không dồn máy.
    with dl._dl_semaphore:
        dl._do_download(job_id, dl._extract_url(video_url), 'mp4', ANALYSIS_QUALITY)
    state = progress_get(job_id) or {}
    out_base = os.path.join(tempfile.gettempdir(), f'vcb_dl_{job_id}')
    target = dl.finished_media_path(out_base, 'mp4')
    if state.get('status') == 'done' and target:
        return target, ''
    dl._cleanup_job_files(out_base)
    return None, state.get('error') or 'trình tải miễn phí thất bại'


def _salvage_partial(path: str) -> Optional[str]:
    """Ghép lại phần đầu của một file mp4 tải dở thành file hợp lệ (copy luồng tới chỗ đứt).

    File dừng giữa chừng vẫn mang header của cả video (ffprobe báo đủ độ dài) nhưng thiếu
    dữ liệu phía sau — gửi thẳng cho Gemini dễ bị từ chối. ffmpeg copy từng gói đọc được rồi
    dừng ở chỗ đứt, cho ra file ngắn hơn nhưng phát trọn vẹn.
    """
    from video_management.views import video_downloader_views as dl
    ffmpeg = dl._get_ffmpeg() or 'ffmpeg'
    out = os.path.splitext(path)[0] + '.part.mp4'
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-err_detect', 'ignore_err', '-i', path, '-c', 'copy', out],
                   capture_output=True, text=True, timeout=120)
    if os.path.isfile(out) and os.path.getsize(out) > 50_000 and dl._media_file_ok(out, 'mp4'):
        return out
    cleanup_path(out)
    return None


def _download_direct(play_url: str, headers: Optional[dict] = None) -> tuple:
    """Tải thẳng file mp4 từ link CDN (link TikHub). Trả (đường dẫn | None, lý do lỗi).

    Không gửi Referer: Douyin chặn theo Referer — gọi từ server không kèm Referer mới được
    (xem tikhub_play_url.py). Xiaohongshu trả link http://, requests tải bình thường.
    Quá DIRECT_DOWNLOAD_DEADLINE hoặc quá MAX_DIRECT_DOWNLOAD_BYTES thì dừng và cứu phần đầu.
    """
    from video_management.views import video_downloader_views as dl
    path = os.path.join(tempfile.gettempdir(), f'vcb_vs_{uuid.uuid4().hex[:12]}.mp4')
    stopped_early = ''
    try:
        deadline = time.monotonic() + DIRECT_DOWNLOAD_DEADLINE
        with requests.get(play_url, headers={'User-Agent': dl.BROWSER_UA, **(headers or {})}, stream=True,
                          timeout=DIRECT_DOWNLOAD_TIMEOUT, allow_redirects=True) as resp:
            if resp.status_code not in (200, 206):
                return None, f'CDN trả HTTP {resp.status_code}'
            written = 0
            with open(path, 'wb') as f:
                for chunk in resp.iter_content(1024 * 512):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    if written > MAX_DIRECT_DOWNLOAD_BYTES:
                        stopped_early = f'video quá lớn (> {MAX_DIRECT_DOWNLOAD_BYTES // (1024 * 1024)}MB)'
                        break
                    if time.monotonic() > deadline:
                        stopped_early = f'CDN quá chậm (quá {DIRECT_DOWNLOAD_DEADLINE}s, mới được {written / 1e6:.0f}MB)'
                        break
        if stopped_early:
            logger.info(f'[VideoScript] dừng tải thẳng sớm: {stopped_early} — cứu phần đầu')
            part = _salvage_partial(path)
            # Chỉ xoá đúng file dở — cleanup_path xoá theo tiền tố tên, sẽ xoá luôn file .part vừa cứu.
            try:
                os.remove(path)
            except OSError:
                pass
            if part:
                return part, ''
            return None, f'{stopped_early}, phần đã tải không dùng được'
        if not dl._media_file_ok(path, 'mp4'):
            raise ValueError('file tải về không phát được')
        return path, ''
    except Exception as e:  # noqa: BLE001 — mọi lỗi đều quy về "đường này hỏng"
        cleanup_path(path)
        return None, str(e) or e.__class__.__name__


def _download_ffmpeg(inputs: list, headers: Optional[dict] = None) -> tuple:
    """Tải bằng ffmpeg: luồng HLS (Reddit) hoặc ghép hình + tiếng tách rời (Bilibili DASH).

    Cắt luôn ở max_seconds() nên không kéo phần thừa của video dài. Ghi mp4 dạng fragment để
    hết hạn DIRECT_DOWNLOAD_DEADLINE mà phải dừng ngang thì phần đã tải vẫn đọc được; sau đó
    đóng gói lại thành mp4 thường (_salvage_partial) cho Gemini.
    """
    from video_management.views import video_downloader_views as dl
    ffmpeg = dl._get_ffmpeg() or 'ffmpeg'
    out = os.path.join(tempfile.gettempdir(), f'vcb_vs_{uuid.uuid4().hex[:12]}.mp4')
    header_blob = ''.join(f'{k}: {v}\r\n' for k, v in {'User-Agent': dl.BROWSER_UA, **(headers or {})}.items())
    cmd = [ffmpeg, '-y', '-v', 'error']
    for url in inputs:
        cmd += ['-headers', header_blob, '-i', url]
    cmd += ['-t', str(max_seconds())]
    if len(inputs) > 1:
        cmd += ['-map', '0:v:0', '-map', '1:a:0']
    cmd += ['-c', 'copy', '-movflags', 'frag_keyframe+empty_moov', out]
    stopped_early = ''
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DIRECT_DOWNLOAD_DEADLINE)
        if proc.returncode != 0 and not os.path.isfile(out):
            return None, f'ffmpeg lỗi: {proc.stderr.strip()[-200:]}'
    except subprocess.TimeoutExpired:
        stopped_early = f'luồng quá chậm (quá {DIRECT_DOWNLOAD_DEADLINE}s)'
        logger.info(f'[VideoScript] ffmpeg dừng sớm: {stopped_early} — cứu phần đầu')
    if not os.path.isfile(out):
        return None, stopped_early or 'ffmpeg không tạo được file'
    part = _salvage_partial(out)
    try:
        os.remove(out)
    except OSError:
        pass
    if part:
        return part, ''
    return None, f'{stopped_early or "file ffmpeg tạo ra"} không dùng được'


def download_for_analysis(video_url: str, platform: str = '', video_id: str = '') -> DownloadedVideo:
    """Tải video về máy để phân tích. Ném VideoDownloadError nếu mọi đường đều hỏng.

    Người gọi PHẢI xoá file (cleanup_video) khi dùng xong.
    """
    platform = (platform or detect_platform(video_url)).lower()
    reasons = []

    path, err = _download_free(video_url)
    if path:
        try:
            return _describe(path, 'free')
        except Exception as e:  # noqa: BLE001
            cleanup_path(path)
            err = str(e)
    reasons.append(f'tải miễn phí: {err}')
    logger.info(f'[VideoScript] tải miễn phí hỏng ({platform}): {err}')

    if platform not in TIKHUB_FALLBACK_PLATFORMS:
        raise VideoDownloadError(reasons)

    from video_management.services.tikhub_download_source import TikHubOutOfCredit, fetch_download_source
    try:
        source = fetch_download_source(platform, video_id=video_id, video_url=video_url)
    except TikHubOutOfCredit:
        reasons.append('TikHub: tài khoản hết tiền')
        raise VideoDownloadError(reasons)
    if not source:
        reasons.append('TikHub: không lấy được nguồn tải')
        raise VideoDownloadError(reasons)

    if source.via == 'ffmpeg':
        path, err = _download_ffmpeg(source.inputs, source.headers)
    else:
        path, err = _download_direct(source.inputs[0], source.headers)
    if path:
        try:
            return _describe(path, 'tikhub')
        except Exception as e:  # noqa: BLE001
            cleanup_path(path)
            err = str(e)
    reasons.append(f'TikHub: {err}')
    raise VideoDownloadError(reasons)


def cleanup_path(path: Optional[str]) -> None:
    if not path:
        return
    base = os.path.splitext(path)[0]
    for f in {path, *glob.glob(base + '.*')}:
        try:
            os.remove(f)
        except OSError:
            pass


def cleanup_video(video: Optional[DownloadedVideo]) -> None:
    if video:
        cleanup_path(video.path)


def script_engine() -> str:
    """'gemini' hoặc 'off'.

    VIDEO_SCRIPT_ENGINE đặt tường minh thì theo nó (off = tắt hẳn, vd khi cần ngừng tốn phí
    Gemini). Không đặt thì có GEMINI_API_KEY là bật — cấu hình khoá đồng nghĩa với "đấu Gemini
    vào", không bắt khai thêm biến thứ hai rồi quên.
    """
    value = str(getattr(settings, 'VIDEO_SCRIPT_ENGINE', '') or os.getenv('VIDEO_SCRIPT_ENGINE', '')).strip().lower()
    if value:
        return 'gemini' if value == 'gemini' else 'off'
    has_key = (getattr(settings, 'GEMINI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '')).strip()
    return 'gemini' if has_key else 'off'


def engine_disabled_reason() -> str:
    """Lý do engine tắt, để BE ghi vào lỗi nếu cách viết cũ cũng hỏng. Rỗng khi cố ý tắt bằng
    VIDEO_SCRIPT_ENGINE — đó là cấu hình, không phải lỗi."""
    if str(getattr(settings, 'VIDEO_SCRIPT_ENGINE', '') or os.getenv('VIDEO_SCRIPT_ENGINE', '')).strip():
        return ''
    return 'Chưa cấu hình GEMINI_API_KEY trên AI Service nên chưa viết được kịch bản từ voice.'
