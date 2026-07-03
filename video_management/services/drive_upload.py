"""Upload media thumbnails lên Google Drive qua BE internal API."""

import logging
import tempfile
import os
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Douyin CDN — một số node chỉ accessible từ IP Trung Quốc
# Dùng timeout ngắn hơn để không block Celery worker quá lâu khi chúng fail
_CHINA_CDN_DOMAINS = (
    'douyinpic.com',
    'bytecdn.cn',
    'ibyteimg.com',
    'douyinstatic.com',
    'douyinvod.com',
)


def _is_drive_url(url: str) -> bool:
    return 'drive.google.com' in url or 'googleusercontent.com' in url


def _is_china_cdn(url: str) -> bool:
    return any(domain in url for domain in _CHINA_CDN_DOMAINS)


_BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://www.douyin.com/',
}


def _download_direct(url: str, timeout: int = 15) -> bytes | None:
    headers = _BROWSER_HEADERS if _is_china_cdn(url) else {}
    try:
        r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        r.raise_for_status()
        data = b''.join(r.iter_content(chunk_size=8192))
        return data if data else None
    except Exception as e:
        logger.warning(f'[DriveUpload] Download failed ({url[:70]}...): {e}')
        return None


def upload_thumbnail_from_url(
    source_url: str,
    filename: str,
    mimetype: str = 'image/jpeg',
) -> str:
    """Tải ảnh thumbnail từ CDN URL rồi upload lên Drive qua BE.

    Trả Drive URL nếu thành công, chuỗi rỗng nếu thất bại (không raise).
    Với Douyin CDN: timeout 8s thay vì 15s — một số node không accessible
    từ ngoài TQ, fail nhanh để không block worker.
    """
    if not source_url:
        return ''
    if _is_drive_url(source_url):
        return source_url

    be_url = getattr(settings, 'BE_INTERNAL_URL', '')
    api_key = getattr(settings, 'INTERNAL_API_KEY', '')
    if not be_url or not api_key:
        logger.warning('[DriveUpload] BE_INTERNAL_URL hoặc INTERNAL_API_KEY chưa cấu hình')
        return ''

    timeout = 8 if _is_china_cdn(source_url) else 15
    image_data = _download_direct(source_url, timeout=timeout)
    if not image_data:
        return ''

    ext = '.jpg' if 'jpeg' in mimetype or 'jpg' in mimetype else '.png'
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(image_data)
            tmp_path = tmp.name

        with open(tmp_path, 'rb') as f:
            resp = requests.post(
                f'{be_url}/api/internal/upload/drive',
                headers={'x-internal-key': api_key},
                files={'file': (filename, f, mimetype)},
                timeout=60,
            )
        resp.raise_for_status()
        drive_url = resp.json().get('url', '')
        logger.info(f'[DriveUpload] ✅ {filename} → {drive_url[:60]}')
        return drive_url

    except Exception as e:
        logger.error(f'[DriveUpload] ❌ Upload thất bại {filename}: {e}')
        return ''
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
