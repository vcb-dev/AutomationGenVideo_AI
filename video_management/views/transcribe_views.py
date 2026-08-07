"""
Video Transcription API — chuyển nội dung spoken trong video thành text.
Dùng yt-dlp để tải video (hỗ trợ TikTok, Instagram, Douyin, ...) rồi
trích audio bằng FFmpeg và gửi lên OpenAI Whisper (whisper-1).
"""
import os
import uuid
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


def transcribe_with_gemini(file_path: str) -> str:
    """
    Upload file directly to Gemini Files API and transcribe using gemini-2.0-flash (or similar).
    Includes file size validation (max 500MB) and cleans up Google file reference afterwards.
    """
    import google.generativeai as genai
    import time

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

    model_name = getattr(settings, 'GEMINI_MODEL', None)
    if not model_name:
        model_name = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
    model_name = str(model_name).strip()

    # 3. Configure and upload
    genai.configure(api_key=api_key)
    logger.info(f"[Gemini Transcribe] Uploading {file_size_mb:.2f}MB file to Gemini Files API...")

    gemini_file = None
    try:
        gemini_file = genai.upload_file(file_path)
        logger.info(f"[Gemini Transcribe] Polling file state for: {gemini_file.name}")
        
        # Poll state until ACTIVE
        while gemini_file.state.name == "PROCESSING":
            time.sleep(2)
            gemini_file = genai.get_file(gemini_file.name)

        if gemini_file.state.name == "FAILED":
            raise Exception("Tải file lên Gemini File API thất bại hoặc định dạng không được hỗ trợ.")

        # 4. Request transcription
        prompt = (
            "Hãy nghe file âm thanh/video này và chuyển toàn bộ nội dung giọng nói thành văn bản tiếng Việt. "
            "Chỉ trả về phần văn bản đã nhận diện được dưới dạng thô, giữ nguyên các đại từ và câu chữ gốc, "
            "không thêm bất kỳ lời giải thích, tiêu đề, hay ghi chú nào khác. "
            "Chú ý viết đúng chính tả các từ: Huy Ca, Viễn Chí Bảo, bạc 925, bạc S925, moissanite, kim cương, CZ, nhẫn, dây chuyền."
        )
        logger.info(f"[Gemini Transcribe] Invoking model {model_name}...")
        model = genai.GenerativeModel(model_name)
        response = model.generate_content([gemini_file, prompt])
        transcript = response.text.strip()
        return transcript

    finally:
        if gemini_file:
            try:
                logger.info(f"[Gemini Transcribe] Cleaning up Google server file: {gemini_file.name}")
                genai.delete_file(gemini_file.name)
            except Exception as e:
                logger.error(f"[Gemini Transcribe] Failed to delete Gemini file {gemini_file.name}: {str(e)}")


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
    """
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

        # 1. Check duration
        duration = _get_media_duration(input_path, ffmpeg_path)
        if duration is None:
            return Response({
                'success': False,
                'error_message': 'Không thể xác định thời lượng của file upload. Vui lòng kiểm tra lại định dạng file.'
            }, status=400)

        if duration > 600:
            return Response({
                'success': False,
                'error_message': f'Thời lượng file quá dài ({round(duration)} giây > 600 giây). Chỉ chấp nhận file dưới 10 phút.'
            }, status=400)

        # 2. Transcribe via Gemini Files API
        transcript = transcribe_with_gemini(input_path)

        # Vietnamese cleanups
        transcript = _normalize_transcript_vi(transcript)

        return Response({
            'success': True,
            'transcript': transcript,
            'duration_seconds': round(duration, 2),
            'char_count': len(transcript)
        })

    except ValueError as ve:
        logger.warning(f"[Transcribe Upload] Validation error: {str(ve)}")
        return Response({
            'success': False,
            'error_message': str(ve)
        }, status=400)
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


