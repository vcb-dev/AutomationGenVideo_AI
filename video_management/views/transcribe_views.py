"""
Video Transcription API — chuyển nội dung spoken trong video thành text.
Dùng yt-dlp để tải video (hỗ trợ TikTok, Instagram, Douyin, ...) rồi
trích audio bằng FFmpeg và gửi lên OpenAI Whisper (whisper-1).
"""
import os
import uuid
import time
import logging
import tempfile
import subprocess
import base64
import glob
import re
import requests as http_requests

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes, parser_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.throttling import SimpleRateThrottle
from typing import Optional, List

logger = logging.getLogger(__name__)

MAX_AUDIO_SIZE_MB = 24   # Whisper giới hạn 25MB
DOWNLOAD_TIMEOUT  = 300  # seconds (yt-dlp) — 5 phút cho video dài hoặc kết nối chậm
MAX_UPLOAD_SIZE_MB = 500  # khớp giới hạn Gemini Files API (transcribe_with_gemini)
# ─────────────────────────────────────────────────────────────────────────────
# NGÂN SÁCH THỜI GIAN CHO /api/content/transcribe-upload/
#
# Trước đây ngân sách (GEMINI_TOTAL_BUDGET = 55s) được neo tại `poll_started_at`,
# tức là tính TỪ SAU khi genai.upload_file() đã xong. Nhưng chính upload_file()
# mới là giai đoạn đắt nhất và tăng theo dung lượng file, nên nó nằm NGOÀI ngân
# sách trong khi timeout của BE lại là đồng hồ treo tường phủ trọn mọi giai đoạn.
# Đo thật bằng video thật (Gemini thật, không mock):
#
#   76.5MB / 297s  ->  upload 43.1s + poll  6.8s + generate  64.4s = 114.3s
#   143.5MB / 557s ->  upload 61.0s + poll 20.2s + generate  28.7s = 109.8s
#   76.5MB / 297s  ->  upload 30.8s + poll  8.9s + generate 195.7s = 239.6s
#
# Cả ba đều vượt xa mốc 60s mà BE chờ => BE luôn tự huỷ trước, để lại đúng lỗi
# "timeout of 60000ms exceeded" thấy trong log. upload_file() ~0.5s/MB, nên riêng
# nó ở mốc trần 200MB đã ~85s, tự nó đã lớn hơn cả ngân sách cũ. Và generate_content
# dao động 28.7s -> 195.7s TRÊN CÙNG MỘT FILE, nên mọi con số dưới đây được chọn theo
# mức xấu nhất quan sát được, không phải mức trung bình.
#
# Nay ngân sách được neo tại LÚC VÀO VIEW và phủ mọi giai đoạn (ghi đĩa, ffprobe,
# upload, polling, generate), đồng thời nhận `timeout_seconds` do BE gửi kèm để
# hai phía không thể lệch nhau âm thầm — cùng quy ước mà /api/ai/transform-content/
# và /api/ai/paast/* đã dùng.
# ─────────────────────────────────────────────────────────────────────────────
# Ngân sách mặc định khi BE không gửi `timeout_seconds` (client gọi thẳng, test...).
TRANSCRIBE_TOTAL_BUDGET_DEFAULT = 420  # seconds — khớp CONTENT_TRANSFORM_TRANSCRIBE_TIMEOUT_MS ở BE
# Chặn trên/dưới cho giá trị BE gửi sang — chặn cả số rác lẫn số quá lớn giữ worker.
TRANSCRIBE_TOTAL_BUDGET_MIN = 60   # seconds
TRANSCRIBE_TOTAL_BUDGET_MAX = 900  # seconds
# Biên chừa lại để serialize + trả response về BE trước khi BE hết kiên nhẫn.
TRANSCRIBE_RESPONSE_MARGIN = 5  # seconds
# Trần riêng cho giai đoạn chờ Gemini xử lý file (PROCESSING -> ACTIVE), để một file
# kẹt PROCESSING không ăn hết ngân sách của giai đoạn sinh transcript phía sau.
# Đo thật: tối đa 20.2s với file 143.5MB — 90s là dư ~4 lần.
GEMINI_FILE_PROCESSING_TIMEOUT = 90  # seconds
# Sàn thời gian luôn dành cho generate_content kể cả khi các giai đoạn trước đã ăn
# gần hết ngân sách — tránh trường hợp timeout bị co về 0 và huỷ ngay lập tức.
# Đo thật: generate_content dao động rất mạnh (28.7s -> 64.4s trên cùng cỡ file),
# nên sàn 10s cũ là vô nghĩa; 30s mới đủ để một lần gọi có cơ hội thành công thật.
GEMINI_GENERATE_MIN_TIMEOUT = 30  # seconds
# TRẦN cho MỖI LẦN gọi generate_content. Trước đây dồn TOÀN BỘ ngân sách còn lại vào 1
# lệnh gọi duy nhất — đo thật: cùng 1 file 30s, lần đầu Gemini "câm" >889s rồi mới lỗi,
# lần thử lại xong trong 61s. Gemini thỉnh thoảng stall ở phía server; chờ hết ngân sách
# 1 lần là vô nghĩa. Cắt ở mốc này rồi GỌI LẠI (xem vòng lặp trong transcribe_with_gemini).
GEMINI_GENERATE_MAX_PER_CALL = 180  # seconds
# Lần THỬ ĐẦU với 1 model CHƯA xác nhận chạy được: timeout ngắn. Model không trả nổi transcript
# trong 60s (kể cả file lớn) thì hoặc hỏng hoặc quá tải — đổi model ngay thay vì phí 180s.
GEMINI_MODEL_PROBE_TIMEOUT = 60  # seconds


def _read_transcribe_budget(request) -> int:
    """
    Ngân sách thời gian (giây) cho cả request transcribe, lấy từ field `timeout_seconds`
    do BE gửi kèm. Giá trị rác/thiếu -> dùng mặc định; giá trị hợp lệ -> kẹp vào
    [MIN, MAX] để một client gọi thẳng không thể giữ worker bao lâu tuỳ thích.
    """
    raw = request.data.get('timeout_seconds')
    if raw in (None, ''):
        return TRANSCRIBE_TOTAL_BUDGET_DEFAULT
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning(f"[Transcribe Upload] timeout_seconds không hợp lệ ({raw!r}) — dùng mặc định")
        return TRANSCRIBE_TOTAL_BUDGET_DEFAULT
    return max(TRANSCRIBE_TOTAL_BUDGET_MIN, min(TRANSCRIBE_TOTAL_BUDGET_MAX, value))


class TranscribeUploadThrottle(SimpleRateThrottle):
    """Giới hạn số lần upload/phút — mỗi request hợp lệ đều tốn 1 lệnh gọi Gemini
    (tính phí) và ghi nguyên file ra đĩa server, cùng kiểu rủi ro với video-downloader."""
    scope = 'transcribe_upload'

    def get_cache_key(self, request, view):
        ident = request.user.pk if request.user and request.user.is_authenticated else self.get_ident(request)
        return f'throttle_transcribe_upload_{ident}'
TRANSCRIBE_GLOSSARY_PROMPT = (
    "Transcribe verbatim, keep original wording, do not summarize. "
    "Prefer correct Vietnamese spelling and punctuation. "
    "Domain terms to preserve: Huy Ca, Viễn Chí Bảo, bạc 925, bạc S925, "
    "moissanite, kim cương, CZ, nhẫn, dây chuyền, lắc tay, bông tai."
)
NOISE_OCR_LINES = {
    'không có chữ trong hình.',
    'không có chữ trong hình',
    'no text in image.',
    'no text in image',
}


@api_view(['POST'])
@permission_classes([AllowAny])
def transcribe_video(request):
    """
    POST /api/content/transcribe/
    Body: { "video_url": "https://..." }
    Response: { "success": true, "transcript": "...", "char_count": N }
    """
    video_url = (request.data.get('video_url') or request.data.get('url') or '').strip()
    language_hint = (request.data.get('language_hint') or '').strip().lower()
    if not video_url:
        logger.warning("[Transcribe] Missing video_url in request data")
        return Response({'success': False, 'error': 'video_url is required'}, status=400)

    anthropic_key = str(getattr(settings, 'ANTHROPIC_API_KEY', '')).strip()
    if not anthropic_key or anthropic_key.startswith('your_'):
        return Response({'success': False, 'error': 'ANTHROPIC_API_KEY not configured'}, status=500)

    ffmpeg_path = _get_ffmpeg()
    if not ffmpeg_path:
        return Response({'success': False, 'error': 'FFmpeg not found. Configure FFMPEG_PATH in .env'}, status=500)

    tmp_dir = tempfile.gettempdir()
    uid = uuid.uuid4().hex[:8]
    audio_path = os.path.join(tmp_dir, f'vcb_audio_{uid}.mp3')
    video_path = os.path.join(tmp_dir, f'vcb_video_{uid}.mp4')

    try:
        # ── Bước 1: Download + extract audio bằng yt-dlp ────────────────────
        logger.info(f"[Transcribe] yt-dlp downloading: {video_url[:80]}...")

        ytdlp = _get_ytdlp()
        logger.info(f"[Transcribe] Found ytdlp: {ytdlp}")
        if ytdlp:
            # OCR-first: luôn tải video file trước để có thể đọc text trong khung hình
            success, error = _download_with_ytdlp(ytdlp, ffmpeg_path, video_url, audio_path, video_path)
        else:
            # Chỉ fallback download direct nếu URL có vẻ là link trực tiếp (cdn, file ext)
            logger.info(f"[Transcribe] ytdlp not found, checking if direct URL: {video_url}")
            is_direct = any(x in video_url.lower() for x in ['.mp4', '.mkv', '.mov', 'cdn', 'media'])
            if is_direct:
                logger.warning("[Transcribe] yt-dlp not found, trying direct download fallback...")
                success, error = _download_direct(ffmpeg_path, video_url, video_path, audio_path)
            else:
                logger.error("[Transcribe] No ytdlp and not a direct link.")
                return Response({'success': False, 'error': 'Hệ thống thiếu công cụ yt-dlp để xử lý link mạng xã hội.'}, status=500)

        logger.info(f"[Transcribe] Download result: success={success}, error={error}")
        if not success:
            # Fallback đặc biệt cho TikTok: resolve direct media URL từ dịch vụ trung gian
            if 'tiktok.com/' in video_url.lower():
                logger.warning("[Transcribe] yt-dlp failed on TikTok URL, trying resolver fallback...")
                direct_url = _resolve_tiktok_direct_url(video_url)
                if direct_url:
                    logger.info(f"[Transcribe] Resolved TikTok direct URL, trying direct download fallback...")
                    success, error = _download_direct(ffmpeg_path, direct_url, video_path, audio_path)
                    logger.info(f"[Transcribe] Direct fallback result: success={success}, error={error}")

            if not success:
                logger.warning(f"[Transcribe] Download failed: {error}")
                return Response({'success': False, 'error': f'Lỗi tải video: {error or "Download thất bại"}. Hãy thử đổi link khác hoặc kiểm tra lại quyền truy cập video.'}, status=400)

        if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 500:
            logger.error(f"[Transcribe] Empty audio file at {audio_path}")
            return Response({'success': False, 'error': 'Audio file trống sau khi download hoặc extract thất bại.'}, status=400)

        audio_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        logger.info(f"[Transcribe] Audio ready: {audio_size_mb:.2f}MB")

        if audio_size_mb > MAX_AUDIO_SIZE_MB:
            return Response({
                'success': False,
                'error': f'Audio quá lớn ({audio_size_mb:.1f}MB > 25MB). Video quá dài, vui lòng chọn video ngắn hơn.'
            }, status=400)

        # ── Bước 2: OpenAI Whisper ───────────────────────────────────────────
        logger.info(f"[Transcribe] Sending {audio_size_mb:.2f}MB to Whisper API...")
        with open(audio_path, 'rb') as f:
            whisper_data = {
                'model': 'whisper-1',
                # Không set language mặc định để giữ khả năng auto-detect.
                # Chỉ set khi FE gửi language_hint rõ ràng (vd: 'vi').
                'response_format': 'verbose_json',  # trả về thêm field 'language'
                'temperature': '0',
                'prompt': TRANSCRIBE_GLOSSARY_PROMPT,
            }
            if language_hint:
                whisper_data['language'] = language_hint

            resp = http_requests.post(
                'https://api.openai.com/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {getattr(settings, "OPENAI_API_KEY", "")}'},
                files={'file': ('audio.mp3', f, 'audio/mpeg')},
                data=whisper_data,
                timeout=120
            )

        if resp.status_code != 200:
            logger.error(f"[Transcribe] Whisper error {resp.status_code}: {resp.text[:300]}")
            return Response({'success': False, 'error': f'Whisper API lỗi {resp.status_code}'}, status=500)

        resp_data = resp.json()
        transcript = resp_data.get('text', '').strip()
        detected_language = resp_data.get('language', 'unknown')   # vd: 'english', 'vietnamese'

        # OCR-first: đọc text trực tiếp từ frame video để giảm lỗi ASR
        ocr_text = ''
        if os.path.exists(video_path):
            ocr_text = _extract_text_from_video_frames(video_path, ffmpeg_path, anthropic_key, uid, tmp_dir)
            if ocr_text:
                logger.info(f"[Transcribe] OCR extracted {len(ocr_text)} chars from video frames")
                fused = _merge_ocr_and_asr(ocr_text, transcript, anthropic_key)
                if fused:
                    transcript = fused

        # Hậu xử lý cho transcript tiếng Việt để sửa lỗi chính tả thương hiệu/thuật ngữ phổ biến.
        if language_hint == 'vi' or detected_language in ('vi', 'vietnamese'):
            transcript = _normalize_transcript_vi(transcript)
            transcript = _refine_transcript_with_claude(transcript, anthropic_key) or transcript

        logger.info(f"[Transcribe] ✅ Done: {len(transcript)} chars | lang={detected_language}")

        return Response({
            'success': True,
            'transcript': transcript,
            'detected_language': detected_language,
            'char_count': len(transcript),
            'audio_size_mb': round(audio_size_mb, 2),
        })


    except Exception as e:
        logger.exception(f"[Transcribe] Unexpected error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)
    finally:
        for path in [audio_path, os.path.join(tmp_dir, f'vcb_video_{uid}.mp4')]:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_ffmpeg() -> str:
    from_settings = str(getattr(settings, 'FFMPEG_PATH', '')).strip()
    if from_settings and os.path.isfile(from_settings):
        return from_settings
    import shutil
    return shutil.which('ffmpeg') or ''


def _get_ytdlp() -> str:
    """Trả về path yt-dlp trong venv của project."""
    import shutil, sys
    # Thử trong cùng venv Python
    venv_scripts = os.path.join(os.path.dirname(sys.executable), 'yt-dlp.exe')
    if os.path.isfile(venv_scripts):
        return venv_scripts
    venv_scripts2 = os.path.join(os.path.dirname(sys.executable), 'yt-dlp')
    if os.path.isfile(venv_scripts2):
        return venv_scripts2
    # Thử PATH
    found = shutil.which('yt-dlp')
    return found or ''


def _download_with_ytdlp(ytdlp: str, ffmpeg: str, url: str, audio_out: str, video_out: str):
    """
    Dùng yt-dlp để download + extract audio thẳng ra mp3.
    Trả về (success: bool, error: str | None)
    """
    ffmpeg_dir = os.path.dirname(ffmpeg) if os.path.isabs(ffmpeg) else ''
    
    base_cmd = [
        ytdlp,
        '--no-playlist',
        '--format', 'bestvideo+bestaudio/best',
        '--merge-output-format', 'mp4',
        '--output', video_out.replace('.mp4', '.%(ext)s'),
        '--no-warnings',
        '--no-check-certificates',
        '--add-header', 'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        '--add-header', 'Accept:text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        '--add-header', 'Accept-Language:vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
        '--add-header', 'Referer:https://www.tiktok.com/',
        '--socket-timeout', '30',
    ]
    if ffmpeg_dir:
        base_cmd.extend(['--ffmpeg-location', ffmpeg_dir])

    def _browser_profile_exists(browser: str) -> bool:
        home = os.path.expanduser('~')
        if browser == 'chrome':
            return os.path.isdir(os.path.join(home, 'Library', 'Application Support', 'Google', 'Chrome'))
        if browser == 'safari':
            # Safari cookies DB thường nằm ở đây trên macOS
            return os.path.exists(os.path.join(home, 'Library', 'Cookies', 'Cookies.binarycookies')) or \
                os.path.exists(os.path.join(home, 'Library', 'Containers', 'com.apple.Safari'))
        return False

    # Retry profiles: TikTok thường chặn theo từng cách trích xuất
    attempt_profiles = [
        [],
        ['--extractor-args', 'tiktok:api_hostname=api16-normal-useast5.us.tiktokv.com'],
        ['--extractor-args', 'tiktok:api_hostname=api22-normal-c-useast1a.tiktokv.com'],
    ]
    # IMPORTANT:
    # Browser cookies access trên macOS thường bị chặn bởi quyền riêng tư (Operation not permitted).
    # Mặc định TẮT để không làm hỏng luồng transcribe đang chạy ổn.
    use_browser_cookies = bool(getattr(settings, 'YTDLP_USE_BROWSER_COOKIES', False))
    if use_browser_cookies:
        if _browser_profile_exists('safari'):
            attempt_profiles.append(['--cookies-from-browser', 'safari'])
        else:
            logger.info("[Transcribe] Skip safari cookies profile (not found)")
        if _browser_profile_exists('chrome'):
            attempt_profiles.append(['--cookies-from-browser', 'chrome'])
        else:
            logger.info("[Transcribe] Skip chrome cookies profile (not found)")
    else:
        logger.info("[Transcribe] Browser cookies attempts disabled (YTDLP_USE_BROWSER_COOKIES=False)")

    def _find_downloaded_file():
        base = video_out.replace('.mp4', '')
        for ext in ['.mp4', '.mkv', '.webm', '.mov']:
            candidate = base + ext
            if os.path.exists(candidate) and os.path.getsize(candidate) > 500:
                return candidate
        return None

    def _compact_error(err_text: str) -> str:
        lines = [ln.strip() for ln in (err_text or '').splitlines() if ln.strip()]
        # Bỏ warning về ssl/python không liên quan trực tiếp đến lỗi download
        filtered = [
            ln for ln in lines
            if 'NotOpenSSLWarning' not in ln
            and 'urllib3' not in ln
            and 'Deprecated Feature: Support for Python version 3.9' not in ln
            and 'warnings.warn(' not in ln
        ]
        return (filtered[-1] if filtered else (lines[-1] if lines else 'Unknown error')).strip()

    last_error = ''
    try:
        for idx, extra_args in enumerate(attempt_profiles, start=1):
            cmd = base_cmd + extra_args + [url]
            logger.info(f"[Transcribe] Executing yt-dlp attempt {idx}/{len(attempt_profiles)}: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
            logger.info(f"[Transcribe] yt-dlp attempt {idx} return code: {result.returncode}")

            found_file = _find_downloaded_file()
            if found_file:
                if found_file != video_out:
                    os.replace(found_file, video_out)
                # Tách audio từ video tải được
                result_audio = subprocess.run(
                    [ffmpeg, '-i', video_out, '-vn', '-acodec', 'libmp3lame',
                     '-ar', '22050', '-ac', '1', '-b:a', '128k', '-y', audio_out],
                    capture_output=True, text=True, timeout=120
                )
                if result_audio.returncode != 0 or not os.path.exists(audio_out):
                    return False, f'FFmpeg extract audio lỗi: {(result_audio.stderr or "")[-200:]}'
                return True, None

            err = result.stderr.strip() if result.stderr else result.stdout.strip()
            compact = _compact_error(err)
            # Không để lỗi "missing browser cookies db" ghi đè nguyên nhân thật
            cookie_missing = 'could not find chrome cookies database' in compact.lower() or \
                'could not find safari cookies' in compact.lower() or \
                ('operation not permitted' in compact.lower() and 'cookies' in compact.lower())
            if not cookie_missing:
                last_error = compact
            logger.warning(f"[Transcribe] yt-dlp attempt {idx} failed: {compact}")

            # Nếu lỗi có tính chất chặn IP rõ ràng thì trả luôn
            if "blocked" in err.lower() or "captcha" in err.lower():
                return False, 'IP server đang bị TikTok chặn. Hãy thử lại sau vài phút hoặc dùng link khác.'

        logger.error(f"[Transcribe] yt-dlp failed all attempts. Last error: {last_error}")
        return False, f'yt-dlp không tạo được file. Chi tiết: {last_error or "Unable to extract video data"}'

    except subprocess.TimeoutExpired:
        return False, f'Tải video timeout (>{DOWNLOAD_TIMEOUT}s). Video quá dài hoặc kết nối chậm. Hãy thử đổi link khác.'
    except Exception as e:
        return False, str(e)


def _download_direct(ffmpeg: str, url: str, video_path: str, audio_out: str):
    """
    Fallback: download trực tiếp bằng HTTP rồi ffmpeg extract audio.
    Chỉ hoạt động với URL CDN trực tiếp (không phải trang web TikTok).
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.tiktok.com/',
        }
        resp = http_requests.get(url, headers=headers, timeout=30, stream=True)
        resp.raise_for_status()

        size_mb = 0
        with open(video_path, 'wb') as f:
            for chunk in resp.iter_content(1024 * 512):
                if chunk:
                    f.write(chunk)
                    size_mb += len(chunk) / (1024 * 1024)
                    if size_mb > 50:
                        break

        if size_mb < 0.05:
            return False, 'URL không phải video trực tiếp. Hãy dùng URL CDN thay vì URL trang TikTok.'

        result = subprocess.run([
            ffmpeg, '-i', video_path, '-vn',
            '-acodec', 'libmp3lame', '-ar', '22050', '-ac', '1', '-b:a', '128k',
            '-y', audio_out
        ], capture_output=True, text=True, timeout=120)

        if result.returncode != 0:
            return False, f'FFmpeg lỗi: {result.stderr[-200:]}'
        return True, None

    except http_requests.exceptions.RequestException as e:
        return False, f'Download lỗi: {str(e)}'
    except subprocess.TimeoutExpired:
        return False, 'FFmpeg timeout'
    finally:
        pass


def _resolve_tiktok_direct_url(video_url: str) -> str:
    """
    Resolve TikTok page URL to direct video URL via public resolver APIs.
    Return empty string if cannot resolve.
    """
    candidates = [
        {
            'name': 'tikwm',
            'method': 'POST',
            'url': 'https://www.tikwm.com/api/',
        },
        {
            'name': 'tikwm2',
            'method': 'POST',
            'url': 'https://tikwm.com/api/',
        },
    ]

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/',
    }

    for c in candidates:
        try:
            logger.info(f"[Transcribe] Trying TikTok resolver: {c['name']}")
            if c['method'] == 'POST':
                resp = http_requests.post(
                    c['url'],
                    headers=headers,
                    data={'url': video_url, 'hd': 1},
                    timeout=20
                )
            else:
                resp = http_requests.get(c['url'], headers=headers, timeout=20)

            if resp.status_code != 200:
                logger.warning(f"[Transcribe] Resolver {c['name']} non-200: {resp.status_code}")
                continue

            data = resp.json()
            # tikwm response usually: {code:0, data:{play:'...', wmplay:'...'}}
            if isinstance(data, dict):
                inner = data.get('data') if isinstance(data.get('data'), dict) else data
                for key in ['play', 'hdplay', 'wmplay', 'url']:
                    v = inner.get(key) if isinstance(inner, dict) else None
                    if isinstance(v, str) and v.startswith('http'):
                        logger.info(f"[Transcribe] Resolver {c['name']} got direct URL via key={key}")
                        return v
        except Exception as e:
            logger.warning(f"[Transcribe] Resolver {c['name']} failed: {e}")
            continue

    logger.warning("[Transcribe] Could not resolve TikTok direct URL from all resolvers")
    return ''


def _extract_text_from_video_frames(video_path: str, ffmpeg_path: str, anthropic_key: str, uid: str, tmp_dir: str) -> str:
    """
    OCR chữ cháy trên khung hình video bằng Claude vision.

    Trước đây thân hàm gọi `_ocr_frame_with_openai(frame, openai_key)` — cả hàm lẫn biến đó
    đều KHÔNG tồn tại (chỉ có `_ocr_frame_with_claude`). Mỗi lần chạy là một NameError, bị
    `except Exception` bên dưới nuốt gọn nên hàm luôn trả chuỗi rỗng và không ai thấy lỗi.
    Đo lại sau khi sửa: cùng một video trước trả 0 ký tự.
    """
    frames_dir = os.path.join(tmp_dir, f'vcb_frames_{uid}')
    os.makedirs(frames_dir, exist_ok=True)
    frame_pattern = os.path.join(frames_dir, 'frame_%03d.jpg')
    try:
        # Lấy khoảng 12 frame đầu, mỗi 2 giây/frame để bắt subtitle overlay
        subprocess.run(
            [ffmpeg_path, '-i', video_path, '-vf', 'fps=1/2,scale=960:-1', '-frames:v', '12', '-q:v', '3', '-y', frame_pattern],
            capture_output=True, text=True, timeout=120
        )
        frames = sorted(glob.glob(os.path.join(frames_dir, 'frame_*.jpg')))
        if not frames:
            return ''

        extracted_lines = []
        for frame in frames:
            txt = _ocr_frame_with_claude(frame, anthropic_key)
            if txt:
                extracted_lines.extend([ln.strip() for ln in txt.split('\n') if ln.strip()])

        # dedupe preserve order
        seen = set()
        deduped = []
        for line in extracted_lines:
            key = re.sub(r'\s+', ' ', line.lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(line)

        return ' '.join(deduped).strip()
    except Exception as e:
        logger.warning(f"[Transcribe] OCR frame extraction failed: {e}")
        return ''
    finally:
        for p in glob.glob(os.path.join(frames_dir, '*')):
            try:
                os.remove(p)
            except Exception:
                pass
        try:
            os.rmdir(frames_dir)
        except Exception:
            pass


def _ocr_frame_with_claude(frame_path: str, anthropic_key: str) -> str:
    """OCR a single frame using Claude Vision."""
    try:
        import base64
        from anthropic import Anthropic
        client = Anthropic(api_key=anthropic_key)
        
        with open(frame_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
        
        # Ba ID cũ ("claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5") đều KHÔNG tồn tại
        # nên mọi khung hình đều trả 400 Bad Request. Haiku 4.5 đứng đầu vì OCR chữ trên ảnh là
        # việc nhẹ, mà hàm này gọi 12 lần cho mỗi video — dùng model to là đốt tiền vô ích.
        models = ["claude-haiku-4-5-20251001", "claude-sonnet-5"]
        response = None
        for m in models:
            try:
                response = client.messages.create(
                    model=m,
                    max_tokens=300,
                    temperature=0,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/jpeg",
                                        "data": img_data,
                                    },
                                },
                                {"type": "text", "text": "Trích xuất toàn bộ văn bản tiếng Việt trong ảnh này. Chỉ trả về văn bản, không giải thích."}
                            ],
                        }
                    ],
                )
                break
            except Exception as e:
                if "not_found_error" in str(e).lower() and m != models[-1]:
                    continue
                raise e
        return response.content[0].text.strip()
    except Exception:
        return ''


def _merge_ocr_and_asr(ocr_text: str, asr_text: str, anthropic_key: str) -> str:
    """
    Hybrid merge: use OCR + ASR, then fuse by LLM with strict constraints.
    """
    ocr_text = (ocr_text or '').strip()
    asr_text = (asr_text or '').strip()
    ocr_text = _remove_ocr_noise_lines(ocr_text)
    asr_text = _remove_ocr_noise_lines(asr_text)
    if not ocr_text:
        return asr_text
    if not asr_text:
        return ocr_text

    fused = _fuse_transcript_with_claude(ocr_text, asr_text, anthropic_key)
    return fused or (ocr_text if len(ocr_text) >= len(asr_text) else asr_text)


def _remove_ocr_noise_lines(text: str) -> str:
    lines = [ln.strip() for ln in (text or '').split('\n') if ln.strip()]
    cleaned = []
    for ln in lines:
        if ln.lower() in NOISE_OCR_LINES:
            continue
        cleaned.append(ln)
    return ' '.join(cleaned).strip()


def _fuse_transcript_with_claude(ocr_text: str, asr_text: str, anthropic_key: str) -> str:
    """
    Fuse OCR + ASR into a higher-accuracy Vietnamese transcript.
    Prioritize domain words and remove OCR artifacts.
    """
    if not ocr_text and not asr_text:
        return ''
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=anthropic_key)
        
        models = ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"]
        response = None
        for m in models:
            try:
                response = client.messages.create(
                    model=m,
                    max_tokens=2048,
                    temperature=0,
                    system=(
                        "Bạn là chuyên gia hợp nhất transcript. "
                        "Nhiệm vụ: hợp nhất OCR và ASR thành 1 transcript tiếng Việt chính xác hơn. "
                        "Giữ nguyên ý và thứ tự nội dung, không thêm mới. "
                        "Ưu tiên đúng thuật ngữ/domain: Huy Ca, Viễn Chí Bảo, bạc 925, S925, CZ, moissanite. "
                        "Loại bỏ các dòng rác như 'Không có chữ trong hình'."
                    ),
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "OCR TEXT:\n"
                                f"{ocr_text}\n\n"
                                "ASR TEXT:\n"
                                f"{asr_text}\n\n"
                                "Trả về DUY NHẤT transcript hợp nhất."
                            )
                        }
                    ],
                )
                break
            except Exception as e:
                if "not_found_error" in str(e).lower() and m != models[-1]:
                    continue
                raise e
        return response.content[0].text.strip()
    except Exception:
        return ''


def _normalize_transcript_vi(text: str) -> str:
    """
    Rule-based cleanup for frequent Vietnamese/domain transcription mistakes.
    Keep minimal and safe (no paraphrasing).
    """
    import re
    t = (text or '').strip()
    if not t:
        return t

    replacements = {
        'VNCHIBA': 'Viễn Chí Bảo',
        'VNCHI BÀ': 'Viễn Chí Bảo',
        'VNCHI BA': 'Viễn Chí Bảo',
        'vnchiba': 'Viễn Chí Bảo',
        'vnc hiba': 'Viễn Chí Bảo',
        'Huy Canh': 'Huy Ca',
        'zikim': 'đính kim',
        'si kim': 'xi kim',
        'hoa tự đẳng': 'hoa tử đằng',
        'trùng hoa': 'cụm hoa',
        'váy bóc': 'váy vóc',
        'zz': '',
    }
    for k, v in replacements.items():
        t = t.replace(k, v)

    # Dọn khoảng trắng
    t = re.sub(r'\s{2,}', ' ', t).strip()
    return t


def _refine_transcript_with_claude(text: str, anthropic_key: str) -> str:
    """
    Optional LLM post-correction for Vietnamese spelling/wording errors using Claude.
    Strictly preserves meaning and sentence order.
    """
    if not text:
        return text
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=anthropic_key)
        
        models = ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"]
        response = None
        for m in models:
            try:
                response = client.messages.create(
                    model=m,
                    max_tokens=2048,
                    temperature=0,
                    system=(
                        "Bạn là biên tập viên transcript. "
                        "Chỉ sửa lỗi chính tả/nhận diện từ sai trong tiếng Việt, "
                        "giữ nguyên thứ tự câu và ý nghĩa, không thêm bớt nội dung."
                    ),
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "Sửa transcript dưới đây, trả về DUY NHẤT transcript đã sửa:\n\n"
                                f"{text}"
                            )
                        }
                    ],
                )
                break
            except Exception as e:
                if "not_found_error" in str(e).lower() and m != models[-1]:
                    continue
                raise e
        return response.content[0].text.strip()
    except Exception:
        return text


def _get_ffprobe() -> str:
    from_settings = str(getattr(settings, 'FFPROBE_PATH', '')).strip()
    if from_settings and os.path.isfile(from_settings):
        return from_settings
    ffmpeg = _get_ffmpeg()
    if not ffmpeg:
        import shutil
        return shutil.which('ffprobe') or 'ffprobe'
    if ffmpeg == 'ffmpeg':
        return 'ffprobe'
    base = ffmpeg.replace('ffmpeg.exe', '').replace('ffmpeg', '')
    out = (base + 'ffprobe.exe') if '.exe' in ffmpeg else (base + 'ffprobe')
    if os.path.isfile(out):
        return out
    import shutil
    return shutil.which('ffprobe') or 'ffprobe'


def _get_media_duration(file_path: str, ffmpeg_path: str) -> Optional[float]:
    ffprobe_path = _get_ffprobe()
    try:
        result = subprocess.run([
            ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', file_path
        ], capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception as e:
        logger.error(f"Error getting duration via ffprobe format: {str(e)}")

    try:
        result = subprocess.run([
            ffprobe_path, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', file_path
        ], capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception as e:
        logger.error(f"Error getting duration via ffprobe stream: {str(e)}")

    try:
        from moviepy.editor import AudioFileClip
        clip = AudioFileClip(file_path)
        duration = clip.duration
        clip.close()
        return duration
    except Exception as e:
        logger.error(f"Error getting duration via MoviePy: {str(e)}")

    return None


# Định dạng audio gửi Gemini: mp3 mono 16kHz 64kbps — thừa cho nhận diện tiếng nói, mà cực nhẹ.
# 30 giây ≈ 240KB, 10 phút ≈ 4.8MB (so với video HEVC 1080x1920 gốc có thể 10-100MB).
_GEMINI_AUDIO_ARGS = ['-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '64k']
_GEMINI_AUDIO_EXTRACT_TIMEOUT = 180  # ffmpeg tách audio — dài dự phòng cho file 10 phút / máy chậm


def extract_audio_for_gemini(input_path: str, ffmpeg_path: str, out_path: str) -> bool:
    """Tách RIÊNG track âm thanh ra mp3 để gửi Gemini thay vì cả video.

    Transcribe chỉ cần tiếng nói. Gửi cả video buộc Gemini phải giải mã + lập chỉ mục
    khung hình — với video HEVC 1080x1920 (dọc) đây là chỗ Gemini hay "đơ" (đo thật: cùng
    1 file 30s, gửi cả video stall >889s qua nhiều lần thử; audio-only xong trong vài giây).
    Đồng thời cắt dung lượng upload ~20-50 lần.

    Trả True nếu ra file audio hợp lệ (>500 byte); False để caller fallback gửi file gốc
    (vd file không có audio stream, container lạ ffmpeg không đọc được).
    """
    try:
        result = subprocess.run(
            [ffmpeg_path, '-i', input_path, *_GEMINI_AUDIO_ARGS, '-y', out_path],
            capture_output=True, text=True, timeout=_GEMINI_AUDIO_EXTRACT_TIMEOUT,
        )
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 500:
            return True
        logger.warning(
            f"[Gemini Transcribe] Tách audio thất bại (rc={result.returncode}) — "
            f"gửi file gốc: {(result.stderr or '')[-300:]}"
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"[Gemini Transcribe] Tách audio quá {_GEMINI_AUDIO_EXTRACT_TIMEOUT}s — gửi file gốc.")
    except OSError as e:
        logger.warning(f"[Gemini Transcribe] Tách audio lỗi ({e}) — gửi file gốc.")
    return False


# Model Gemini cho transcribe. THỨ TỰ ƯU TIÊN. `gemini-flash-lite-latest` đứng đầu: đo thật
# transcribe audio 12s xong trong ~7s, rẻ nhất, quá đủ cho nhận diện tiếng nói.
#
# Vì sao cần LIST + fallback: alias `-latest` của Google TRÔI theo thời gian và có thể trỏ vào
# model đang lỗi/quá tải với 1 API key cụ thể. Đo thật 2026-08: key hiện tại gọi
# `gemini-flash-latest` (giá trị .env cũ) → DeadlineExceeded kể cả prompt "say hello"; còn
# `gemini-2.5-flash` → 404 "no longer available to new users". `gemini-3.5-flash` /
# `gemini-flash-lite-latest` → 2-7s, chuẩn. Fallback tự động sang model chạy được thay vì để
# cả tính năng chết theo 1 alias.
_GEMINI_TRANSCRIBE_MODELS = ['gemini-flash-lite-latest', 'gemini-3.5-flash', 'gemini-2.5-flash']
# Model đầu tiên chạy được trong process này — thử trước ở các lần sau, khỏi dò lại từ đầu.
_gemini_working_model: Optional[str] = None


def _gemini_model_candidates() -> list:
    """Danh sách model để thử, không trùng: [model đã biết chạy được] + [GEMINI_MODEL cấu hình]
    + [_GEMINI_TRANSCRIBE_MODELS mặc định]."""
    configured = (getattr(settings, 'GEMINI_MODEL', None) or os.getenv('GEMINI_MODEL', '') or '').strip()
    out: list = []
    for m in [_gemini_working_model, configured, *_GEMINI_TRANSCRIBE_MODELS]:
        if m and m not in out:
            out.append(m)
    return out


def transcribe_with_gemini(file_path: str, deadline: Optional[float] = None, heartbeat=None) -> str:
    """
    Upload file (nên là audio-only — xem extract_audio_for_gemini) lên Gemini Files API và
    sinh transcript. Thử lần lượt các model trong _gemini_model_candidates() + retry per-call.
    Kiểm tra dung lượng (≤500MB) và dọn file trên server Google sau khi xong.

    `deadline` là MỐC THỜI GIAN TUYỆT ĐỐI (time.time() + ngân sách còn lại) mà toàn bộ
    hàm này phải kết thúc trước — do người gọi tính từ lúc vào view, nên nó đã trừ sẵn
    phần thời gian đã tiêu cho ghi đĩa + ffprobe. Truyền None = không giới hạn (chỉ dùng
    cho script đo/khảo sát, không dùng ở đường request thật).

    `heartbeat(msg=None)` (tuỳ chọn): gọi ở đầu mỗi giai đoạn để job nền làm mới `updated_at`
    trong progress store — nhờ vậy một job đang chờ Gemini không bị nhìn nhầm là "chết".
    """
    import google.generativeai as genai
    from google.api_core import exceptions as google_api_exceptions
    import time

    def _remaining() -> float:
        """Số giây còn lại trước deadline (vô hạn nếu không đặt deadline)."""
        if deadline is None:
            return float('inf')
        return deadline - time.time()

    def _beat(msg=None) -> None:
        if heartbeat:
            try:
                heartbeat(msg)
            except Exception:  # noqa: BLE001 — heartbeat hỏng không được làm chết transcribe
                pass

    # 1. Validate file size on disk before upload
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if file_size_mb > 500:
        raise ValueError(f"Dung lượng tập tin vượt quá giới hạn cho phép ({file_size_mb:.1f}MB > 500MB).")

    # 2. Load settings
    api_key = getattr(settings, 'GEMINI_API_KEY', '').strip()
    if not api_key:
        api_key = os.getenv('GEMINI_API_KEY', '').strip()
    if not api_key:
        raise ValueError("Hệ thống chưa cấu hình GEMINI_API_KEY trên AI Service.")

    model_candidates = _gemini_model_candidates() or ['gemini-flash-lite-latest']

    # 3. Configure and upload
    genai.configure(api_key=api_key)
    logger.info(f"[Gemini Transcribe] Uploading {file_size_mb:.2f}MB file to Gemini Files API...")

    gemini_file = None
    try:
        _beat("Đang tải file lên Gemini...")
        upload_started_at = time.time()
        gemini_file = genai.upload_file(file_path)
        upload_elapsed = time.time() - upload_started_at
        _beat("Đã tải xong, đang chờ Gemini xử lý...")
        logger.info(
            f"[Gemini Transcribe] upload_file xong sau {upload_elapsed:.1f}s "
            f"({file_size_mb:.1f}MB); ngân sách còn {_remaining():.1f}s. "
            f"Polling file state for: {gemini_file.name}"
        )

        # upload_file() không nhận timeout riêng nên không cắt được giữa chừng; kiểm tra
        # NGAY SAU khi nó xong. Hết ngân sách ở đây thì dừng luôn thay vì lao tiếp vào
        # generate_content để rồi bị BE cắt ngang — cách đó vừa tốn thêm 1 lệnh gọi Gemini
        # tính phí, vừa khiến FE chỉ nhận được lỗi mạng chung chung thay vì lý do thật.
        if _remaining() <= GEMINI_GENERATE_MIN_TIMEOUT:
            raise TimeoutError(
                f"Tải {file_size_mb:.1f}MB lên Gemini đã mất {upload_elapsed:.0f}s, "
                f"không còn đủ thời gian để sinh transcript."
            )

        # Poll state until ACTIVE — trần thời gian là min(trần riêng của giai đoạn này,
        # phần ngân sách còn lại sau khi đã chừa sàn cho generate_content) để một file kẹt
        # PROCESSING không nuốt luôn phần thời gian của bước sinh transcript. Thoát bằng
        # exception nên vẫn đi qua khối finally bên dưới (xoá file trên Gemini) và finally
        # của transcribe_upload (xoá file tạm trên đĩa).
        poll_started_at = time.time()
        poll_budget = min(
            GEMINI_FILE_PROCESSING_TIMEOUT,
            max(0.0, _remaining() - GEMINI_GENERATE_MIN_TIMEOUT),
        )
        while gemini_file.state.name == "PROCESSING":
            if time.time() - poll_started_at > poll_budget:
                raise TimeoutError(
                    f"Gemini xử lý file quá lâu (>{poll_budget:.0f}s) và vẫn ở trạng thái PROCESSING."
                )
            _beat("Gemini đang xử lý file...")
            time.sleep(2)
            gemini_file = genai.get_file(gemini_file.name)

        if gemini_file.state.name == "FAILED":
            raise Exception("Tải file lên Gemini File API thất bại hoặc định dạng không được hỗ trợ.")

        # 4. Request transcription — CẮT MỖI LẦN GỌI ở GEMINI_GENERATE_MAX_PER_CALL rồi GỌI LẠI,
        # thay vì dồn cả ngân sách vào 1 lệnh gọi. Gemini thỉnh thoảng stall server-side (đo
        # thật: cùng 1 file, lần đầu >889s không phản hồi, lần 2 xong trong 61s) — thử lại rẻ
        # hơn và nhanh hơn nhiều so với chờ 1 lần cho tới hết giờ.
        prompt = (
            "Hãy nghe file âm thanh/video này và chuyển toàn bộ nội dung giọng nói thành văn bản tiếng Việt. "
            "Chỉ trả về phần văn bản đã nhận diện được dưới dạng thô, giữ nguyên các đại từ và câu chữ gốc, "
            "không thêm bất kỳ lời giải thích, tiêu đề, hay ghi chú nào khác. "
            "Chú ý viết đúng chính tả các từ: Huy Ca, Viễn Chí Bảo, bạc 925, bạc S925, moissanite, kim cương, CZ, nhẫn, dây chuyền."
        )
        global _gemini_working_model
        attempt = 0
        last_err = None
        # Thử lần lượt từng model ứng viên:
        #  - NotFound/PermissionDenied  → model sai với key này, đổi model NGAY.
        #  - Model CHƯA xác nhận + lần đầu: timeout ngắn (PROBE); DeadlineExceeded → đổi model
        #    ngay (khỏi phí 180s cho model hỏng/quá tải như `gemini-flash-latest` hiện tại).
        #  - Model ĐÃ xác nhận chạy được (_gemini_working_model): full timeout + tối đa 3 lần thử.
        for model_name in model_candidates:
            is_known = model_name == _gemini_working_model
            model = genai.GenerativeModel(model_name)
            max_tries = 3 if is_known else 2
            for k in range(1, max_tries + 1):
                attempt += 1
                budget_left = _remaining() - TRANSCRIBE_RESPONSE_MARGIN
                if budget_left < GEMINI_GENERATE_MIN_TIMEOUT:
                    raise TimeoutError(
                        f"Gemini sinh transcript quá lâu — đã thử {attempt - 1} lượt, không còn ngân sách."
                    ) from last_err
                probe = (not is_known) and k == 1
                per_call = min(GEMINI_MODEL_PROBE_TIMEOUT if probe else GEMINI_GENERATE_MAX_PER_CALL, budget_left)
                logger.info(
                    f"[Gemini Transcribe] generate_content [{model_name}]{' (probe)' if probe else ''} "
                    f"lần {attempt} (timeout {per_call:.0f}s, ngân sách còn {_remaining():.0f}s)..."
                )
                _beat(f"Đang sinh transcript (lần thử {attempt})...")
                try:
                    response = model.generate_content(
                        [gemini_file, prompt],
                        request_options={'timeout': per_call},
                    )
                    _gemini_working_model = model_name
                    return response.text.strip()
                except (google_api_exceptions.NotFound, google_api_exceptions.PermissionDenied) as ce:
                    last_err = ce
                    logger.warning(f"[Gemini Transcribe] model {model_name} không dùng được với key này ({ce}) — đổi model.")
                    break
                except (google_api_exceptions.DeadlineExceeded, google_api_exceptions.RetryError) as ge:
                    last_err = ge
                    logger.warning(
                        f"[Gemini Transcribe] [{model_name}] lần {attempt} timeout ~{per_call:.0f}s "
                        f"(còn {_remaining():.0f}s)."
                    )
                    if probe:
                        break  # probe hỏng → đổi model ngay, không thử lại model này

        raise TimeoutError(
            f"Gemini không sinh được transcript sau khi thử {len(model_candidates)} model "
            f"({', '.join(model_candidates)}). Có thể API key đang bị giới hạn quota. Lỗi cuối: {last_err}"
        ) from last_err

    finally:
        if gemini_file:
            try:
                logger.info(f"[Gemini Transcribe] Cleaning up Google server file: {gemini_file.name}")
                genai.delete_file(gemini_file.name)
            except Exception as e:
                logger.error(f"[Gemini Transcribe] Failed to delete Gemini file {gemini_file.name}: {str(e)}")


def run_transcribe_upload_core(
    input_path: str,
    ffmpeg_path: str,
    *,
    deadline: Optional[float],
    request_started_at: float,
    total_budget: int,
    check_cancel=None,
    heartbeat=None,
) -> dict:
    """Chạy phần LÕI của transcribe-upload trên một file ĐÃ nằm sẵn trên đĩa.

    Đo thời lượng → Gemini (Files API) → chuẩn hoá tiếng Việt. KHÔNG đụng tới
    request/response và KHÔNG xoá file tạm (người gọi lo). Trả về một dict đã chuẩn
    hoá để cả `transcribe_upload` (view đồng bộ) lẫn job nền content-transform dùng
    CHUNG một đường map kết quả — tránh mỗi bên tự dựng lại logic phân loại lỗi:

      thành công → {'success': True, 'transcript', 'duration_seconds', 'char_count'}
      lỗi        → {'success': False, 'error_message', 'status_code': 400|499|500|504}

    `check_cancel` (tuỳ chọn): callable trả True khi người dùng đã huỷ job — kiểm
    trước khi lao vào lệnh gọi Gemini tính phí.
    """
    try:
        duration = _get_media_duration(input_path, ffmpeg_path)
        if duration is None:
            return {
                'success': False, 'status_code': 400,
                'error_message': 'Không thể xác định thời lượng của file upload. Vui lòng kiểm tra lại định dạng file.',
            }

        if duration > 600:
            return {
                'success': False, 'status_code': 400,
                'error_message': f'Thời lượng file quá dài ({round(duration)} giây > 600 giây). Chỉ chấp nhận file dưới 10 phút.',
            }

        prep_elapsed = time.time() - request_started_at
        remaining = 'vô hạn' if deadline is None else f'{deadline - time.time():.1f}s'
        logger.info(
            f"[Transcribe Upload] {os.path.getsize(input_path) / (1024 * 1024):.1f}MB / {duration:.0f}s — "
            f"chuẩn bị mất {prep_elapsed:.1f}s; ngân sách {total_budget}s, còn {remaining} cho Gemini."
        )

        if check_cancel and check_cancel():
            return {'success': False, 'status_code': 499, 'error_message': 'Đã huỷ bởi người dùng.'}

        # Tách audio-only TRƯỚC khi gửi Gemini — bước giảm tải lớn nhất (xem extract_audio_for_gemini).
        if heartbeat:
            heartbeat('Đang tách âm thanh...')
        audio_path = os.path.splitext(input_path)[0] + '.gemini16k.mp3'
        gemini_input = input_path
        if extract_audio_for_gemini(input_path, ffmpeg_path, audio_path):
            gemini_input = audio_path
            logger.info(
                f"[Transcribe Upload] Gửi Gemini audio-only {os.path.getsize(audio_path) / 1024:.0f}KB "
                f"(thay vì {os.path.getsize(input_path) / (1024 * 1024):.1f}MB cả video)."
            )
        else:
            audio_path = None  # không có file audio để dọn

        try:
            transcript = transcribe_with_gemini(gemini_input, deadline=deadline, heartbeat=heartbeat)
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
        transcript = _normalize_transcript_vi(transcript)

        logger.info(
            f"[Transcribe Upload] Hoàn tất sau {time.time() - request_started_at:.1f}s "
            f"(ngân sách {total_budget}s) — {len(transcript)} ký tự."
        )
        return {
            'success': True,
            'transcript': transcript,
            'duration_seconds': round(duration, 2),
            'char_count': len(transcript),
        }

    except ValueError as ve:
        logger.warning(f"[Transcribe Upload] Validation error: {str(ve)}")
        return {'success': False, 'status_code': 400, 'error_message': str(ve)}
    except TimeoutError as te:
        # Hết ngân sách ở một trong các giai đoạn Gemini (upload / PROCESSING / sinh
        # transcript) — 504 kèm lý do THẬT của giai đoạn đó.
        elapsed = time.time() - request_started_at
        logger.error(
            f"[Transcribe Upload] Hết ngân sách sau {elapsed:.1f}s/{total_budget}s: {str(te)}"
        )
        return {
            'success': False, 'status_code': 504,
            'error_message': (
                f'Xử lý file quá lâu (đã chạy {elapsed:.0f}s). {str(te)} '
                'Vui lòng thử lại với file ngắn/nhẹ hơn.'
            ),
        }
    except Exception as e:
        logger.exception(f"[Transcribe Upload] Unexpected error: {str(e)}")
        return {
            'success': False, 'status_code': 500,
            'error_message': f'Lỗi hệ thống trong quá trình xử lý: {str(e)}',
        }


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([TranscribeUploadThrottle])
@parser_classes([MultiPartParser, FormParser])
def transcribe_upload(request):
    """
    POST /api/content/transcribe-upload/
    Form Data:
        - file: Uploaded video/audio file
    Response: { "success": true, "transcript": "...", "duration_seconds": N, "char_count": N }

    Yêu cầu đăng nhập + throttle riêng — mỗi request hợp lệ tốn 1 lệnh gọi Gemini
    (tính phí) và ghi nguyên file ra đĩa server trước khi kiểm tra thời lượng,
    khác transcribe_video() (AllowAny) chỉ nhận URL rồi tự tải qua yt-dlp.

    Form field tuỳ chọn `timeout_seconds`: ngân sách NGOÀI mà BE thực sự chờ request
    này. Nhận từ BE để hai phía không lệch nhau âm thầm — cùng quy ước đã dùng ở
    /api/ai/transform-content/ và /api/ai/paast/*.
    """
    # Neo ngân sách NGAY khi vào view: mọi giai đoạn sau đây (ghi file ra đĩa, ffprobe,
    # upload lên Gemini, polling, sinh transcript) đều nằm trong cùng một đồng hồ, đúng
    # như cách timeout phía BE đếm. Neo muộn hơn là tái lập đúng lỗi cũ.
    request_started_at = time.time()
    total_budget = _read_transcribe_budget(request)
    deadline = request_started_at + total_budget - TRANSCRIBE_RESPONSE_MARGIN

    uploaded_file = request.FILES.get('file')
    if not uploaded_file:
        return Response({'success': False, 'error_message': 'No file uploaded'}, status=400)

    file_size_mb = uploaded_file.size / (1024 * 1024)
    if file_size_mb > MAX_UPLOAD_SIZE_MB:
        return Response({
            'success': False,
            'error_message': f'Dung lượng tập tin vượt quá giới hạn cho phép ({file_size_mb:.1f}MB > {MAX_UPLOAD_SIZE_MB}MB).'
        }, status=400)

    ffmpeg_path = _get_ffmpeg()
    if not ffmpeg_path:
        return Response({'success': False, 'error_message': 'FFmpeg not found on server'}, status=500)

    temp_dir = tempfile.gettempdir()
    uid = uuid.uuid4().hex[:8]
    ext = os.path.splitext(uploaded_file.name)[1].lower() or '.mp4'
    input_path = os.path.join(temp_dir, f'vcb_upload_{uid}{ext}')

    try:
        # Save file to disk
        with open(input_path, 'wb+') as dest:
            for chunk in uploaded_file.chunks():
                dest.write(chunk)

        # Phần đo thời lượng + Gemini + chuẩn hoá đã tách ra run_transcribe_upload_core()
        # để job nền content-transform dùng chung — xem docstring hàm đó.
        result = run_transcribe_upload_core(
            input_path,
            ffmpeg_path,
            deadline=deadline,
            request_started_at=request_started_at,
            total_budget=total_budget,
        )
        if result.get('success'):
            return Response({
                'success': True,
                'transcript': result['transcript'],
                'duration_seconds': result['duration_seconds'],
                'char_count': result['char_count'],
            })
        return Response(
            {'success': False, 'error_message': result['error_message']},
            status=result.get('status_code', 500),
        )

    except Exception as e:
        logger.exception(f"[Transcribe Upload] Unexpected error: {str(e)}")
        return Response({
            'success': False,
            'error_message': f'Lỗi hệ thống trong quá trình xử lý: {str(e)}'
        }, status=500)

    finally:
        try:
            if input_path and os.path.exists(input_path):
                os.remove(input_path)
        except Exception as ex:
            logger.error(f"[Transcribe Upload] Failed to delete temp file {input_path}: {str(ex)}")


