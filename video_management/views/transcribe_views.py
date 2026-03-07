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
import requests as http_requests

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

logger = logging.getLogger(__name__)

MAX_AUDIO_SIZE_MB = 24   # Whisper giới hạn 25MB
DOWNLOAD_TIMEOUT  = 60   # seconds (yt-dlp)


@api_view(['POST'])
@permission_classes([AllowAny])
def transcribe_video(request):
    """
    POST /api/content/transcribe/
    Body: { "video_url": "https://..." }
    Response: { "success": true, "transcript": "...", "char_count": N }
    """
    video_url = (request.data.get('video_url') or request.data.get('url') or '').strip()
    if not video_url:
        logger.warning("[Transcribe] Missing video_url in request data")
        return Response({'success': False, 'error': 'video_url is required'}, status=400)

    openai_key = str(getattr(settings, 'OPENAI_API_KEY', '')).strip()
    if not openai_key:
        return Response({'success': False, 'error': 'OPENAI_API_KEY not configured'}, status=500)

    ffmpeg_path = _get_ffmpeg()
    if not ffmpeg_path:
        return Response({'success': False, 'error': 'FFmpeg not found. Configure FFMPEG_PATH in .env'}, status=500)

    tmp_dir = tempfile.gettempdir()
    uid = uuid.uuid4().hex[:8]
    audio_path = os.path.join(tmp_dir, f'vcb_audio_{uid}.mp3')

    try:
        # ── Bước 1: Download + extract audio bằng yt-dlp ────────────────────
        logger.info(f"[Transcribe] yt-dlp downloading: {video_url[:80]}...")

        ytdlp = _get_ytdlp()
        logger.info(f"[Transcribe] Found ytdlp: {ytdlp}")
        if ytdlp:
            # Dùng yt-dlp: vừa download vừa extract audio, output thẳng mp3
            success, error = _download_with_ytdlp(ytdlp, ffmpeg_path, video_url, audio_path)
        else:
            # Chỉ fallback download direct nếu URL có vẻ là link trực tiếp (cdn, file ext)
            logger.info(f"[Transcribe] ytdlp not found, checking if direct URL: {video_url}")
            is_direct = any(x in video_url.lower() for x in ['.mp4', '.mkv', '.mov', 'cdn', 'media'])
            if is_direct:
                logger.warning("[Transcribe] yt-dlp not found, trying direct download fallback...")
                success, error = _download_direct(ffmpeg_path, video_url, tmp_dir, uid, audio_path)
            else:
                logger.error("[Transcribe] No ytdlp and not a direct link.")
                return Response({'success': False, 'error': 'Hệ thống thiếu công cụ yt-dlp để xử lý link mạng xã hội.'}, status=500)

        logger.info(f"[Transcribe] Download result: success={success}, error={error}")
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
            resp = http_requests.post(
                'https://api.openai.com/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {openai_key}'},
                files={'file': ('audio.mp3', f, 'audio/mpeg')},
                data={
                    'model': 'whisper-1',
                    # Không set language → Whisper tự detect ngôn ngữ gốc video
                    'response_format': 'verbose_json',  # trả về thêm field 'language'
                },
                timeout=120
            )

        if resp.status_code != 200:
            logger.error(f"[Transcribe] Whisper error {resp.status_code}: {resp.text[:300]}")
            return Response({'success': False, 'error': f'Whisper API lỗi {resp.status_code}'}, status=500)

        resp_data = resp.json()
        transcript = resp_data.get('text', '').strip()
        detected_language = resp_data.get('language', 'unknown')   # vd: 'english', 'vietnamese'
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


def _download_with_ytdlp(ytdlp: str, ffmpeg: str, url: str, audio_out: str):
    """
    Dùng yt-dlp để download + extract audio thẳng ra mp3.
    Trả về (success: bool, error: str | None)
    """
    ffmpeg_dir = os.path.dirname(ffmpeg) if os.path.isabs(ffmpeg) else ''
    
    cmd = [
        ytdlp,
        '--no-playlist',
        '--format', 'bestaudio/best',
        '--extract-audio',
        '--audio-format', 'mp3',
        '--audio-quality', '64K',
        '--output', audio_out.replace('.mp3', '.%(ext)s'),
        '--no-warnings',
        '--no-check-certificates',
        '--add-header', 'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        '--add-header', 'Accept:text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        '--add-header', 'Accept-Language:vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
        '--socket-timeout', '30',
        url
    ]
    if ffmpeg_dir:
        cmd.extend(['--ffmpeg-location', ffmpeg_dir])

    logger.info(f"[Transcribe] Executing yt-dlp: {' '.join(cmd)}")
    try:
        # Chạy yt-dlp và lấy cả stdout để debug nếu cần
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
        logger.info(f"[Transcribe] yt-dlp finished with return code: {result.returncode}")
        
        # Tìm file thực tế được tạo ra (vì yt-dlp có thể output định dạng khác trước khi convert)
        base = audio_out.replace('.mp3', '')
        found_file = None
        for ext in ['.mp3', '.m4a', '.webm', '.ogg', '.opus', '.mp4']:
            candidate = base + ext
            if os.path.exists(candidate) and os.path.getsize(candidate) > 500:
                found_file = candidate
                break

        if found_file:
            if found_file != audio_out:
                # Force convert sang mp3 chuẩn cho Whisper
                subprocess.run(
                    [ffmpeg, '-i', found_file, '-acodec', 'libmp3lame',
                     '-ar', '16000', '-ac', '1', '-b:a', '64k', '-y', audio_out],
                    capture_output=True, timeout=60
                )
                if os.path.exists(found_file): os.remove(found_file)
            return True, None

        # Lỗi chi tiết từ yt-dlp
        err = result.stderr.strip() if result.stderr else result.stdout.strip()
        if "blocked" in err.lower():
            return False, 'IP server đang bị TikTok chặn. Hãy thử lại sau vài phút hoặc dùng link từ nền tảng khác.'
        
        logger.error(f"[Transcribe] yt-dlp failed completely. Stderr: {err}")
        return False, f'yt-dlp không tạo được file. Chi tiết: {err[-200:]}'

    except subprocess.TimeoutExpired:
        return False, 'Tải video timeout (>60s). Video quá dài hoặc kết nối chậm.'
    except Exception as e:
        return False, str(e)


def _download_direct(ffmpeg: str, url: str, tmp_dir: str, uid: str, audio_out: str):
    """
    Fallback: download trực tiếp bằng HTTP rồi ffmpeg extract audio.
    Chỉ hoạt động với URL CDN trực tiếp (không phải trang web TikTok).
    """
    video_path = os.path.join(tmp_dir, f'vcb_video_{uid}.mp4')
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
            '-acodec', 'libmp3lame', '-ar', '16000', '-ac', '1', '-b:a', '64k',
            '-y', audio_out
        ], capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            return False, f'FFmpeg lỗi: {result.stderr[-200:]}'
        return True, None

    except http_requests.exceptions.RequestException as e:
        return False, f'Download lỗi: {str(e)}'
    except subprocess.TimeoutExpired:
        return False, 'FFmpeg timeout'
    finally:
        if os.path.exists(video_path):
            os.remove(video_path)
