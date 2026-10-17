"""Tải file model của Tạo ảnh SP (model ONNX + thư viện tư thế, ~580MB — không nằm trong repo) từ thư mục Drive công ty
về máy chủ. Chạy nền lúc gunicorn khởi động; lượt gọi đầu tiên chờ tải xong. File đã có đúng cỡ thì bỏ qua; file
`<tên>.tar` được giải nén thành thư mục `<tên>`.

Bật khi có PRODUCT_IMAGE_MODELS_DRIVE_FOLDER_ID; khi đó bắt buộc PRODUCT_IMAGE_MODELS_DIR và GOOGLE_DRIVE_CLIENT_ID /
GOOGLE_DRIVE_CLIENT_SECRET / GOOGLE_DRIVE_REFRESH_TOKEN. Không đặt thì coi file model đã có sẵn trên máy (chạy local)."""
import fcntl
import json
import logging
import os
import shutil
import sys
import tarfile
import threading
import urllib.parse
import urllib.request

from video_management.services.product_image_service import ProductImageConfigError

DRIVE_FOLDER_ENV = 'PRODUCT_IMAGE_MODELS_DRIVE_FOLDER_ID'
MODELS_DIR_ENV = 'PRODUCT_IMAGE_MODELS_DIR'
CREDENTIAL_ENVS = ('GOOGLE_DRIVE_CLIENT_ID', 'GOOGLE_DRIVE_CLIENT_SECRET', 'GOOGLE_DRIVE_REFRESH_TOKEN')
FOLDER_MIME = 'application/vnd.google-apps.folder'
LOCK_FILE = '.sync.lock'
ARCHIVE_SUFFIX = '.tar'  # thư mục nhiều file nhỏ (thư viện tư thế) gói thành 1 file cho tải nhanh
CHUNK_BYTES = 1 << 20

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_synced = False


def read_settings():
    """(id thư mục Drive, thư mục đích, (client id, secret, refresh token)) — None nếu không bật tải từ Drive."""
    folder_id = (os.environ.get(DRIVE_FOLDER_ENV) or '').strip()
    if not folder_id:
        return None
    target = (os.environ.get(MODELS_DIR_ENV) or '').strip()
    credentials = tuple((os.environ.get(name) or '').strip() for name in CREDENTIAL_ENVS)
    missing = ([MODELS_DIR_ENV] if not target else []) + [n for n, v in zip(CREDENTIAL_ENVS, credentials) if not v]
    if missing:
        raise ProductImageConfigError(f'Đã đặt {DRIVE_FOLDER_ENV} nhưng thiếu {", ".join(missing)} trên AI Service.')
    return folder_id, target, credentials


def _access_token(client_id, client_secret, refresh_token):
    data = urllib.parse.urlencode({'client_id': client_id, 'client_secret': client_secret,
                                   'refresh_token': refresh_token, 'grant_type': 'refresh_token'}).encode()
    with urllib.request.urlopen('https://oauth2.googleapis.com/token', data, timeout=30) as res:
        return json.load(res)['access_token']


def _open(url, token):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'}), timeout=300)


def list_files(token, folder_id, prefix=''):
    """[(đường dẫn tương đối, id, cỡ)] mọi file trong thư mục Drive (cả thư mục con)."""
    files, page = [], None
    while True:
        query = urllib.parse.quote(f"'{folder_id}' in parents and trashed = false")
        url = (f'https://www.googleapis.com/drive/v3/files?q={query}&pageSize=1000&supportsAllDrives=true'
               '&includeItemsFromAllDrives=true&fields=nextPageToken,files(id,name,mimeType,size)')
        if page:
            url += f'&pageToken={page}'
        with _open(url, token) as res:
            body = json.load(res)
        for item in body.get('files', []):
            path = f"{prefix}{item['name']}"
            if item['mimeType'] == FOLDER_MIME:
                files += list_files(token, item['id'], f'{path}/')
            else:
                files.append((path, item['id'], int(item.get('size') or 0)))
        page = body.get('nextPageToken')
        if not page:
            return files


def _download(token, file_id, dest):
    tmp = f'{dest}.part'
    with _open(f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true', token) as res, \
            open(tmp, 'wb') as out:
        while chunk := res.read(CHUNK_BYTES):
            out.write(chunk)
    os.replace(tmp, dest)


def extract(archive):
    """`<tên>.tar` → thư mục `<tên>` cạnh nó (thay hẳn bản cũ)."""
    out = archive[:-len(ARCHIVE_SUFFIX)]
    tmp = f'{out}.extracting'
    shutil.rmtree(tmp, ignore_errors=True)
    with tarfile.open(archive) as tar:
        tar.extractall(tmp, filter='data')
    shutil.rmtree(out, ignore_errors=True)
    os.replace(tmp, out)


def sync(folder_id, target, credentials):
    """Tải file còn thiếu / khác cỡ (file .tar thì giải nén) → số file đã tải. Khoá file để các worker không tải trùng."""
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, LOCK_FILE), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        token = _access_token(*credentials)
        downloaded = 0
        for path, file_id, size in list_files(token, folder_id):
            dest = os.path.join(target, path)
            fresh = os.path.isfile(dest) and os.path.getsize(dest) == size
            if not fresh:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                _download(token, file_id, dest)
                downloaded += 1
            if dest.endswith(ARCHIVE_SUFFIX) and (not fresh or not os.path.isdir(dest[:-len(ARCHIVE_SUFFIX)])):
                extract(dest)
        return downloaded


def ensure_models():
    """Gọi trước khi đọc đường dẫn model: tải từ Drive nếu được bật (mỗi tiến trình một lần)."""
    global _synced
    if _synced:
        return
    with _lock:
        if _synced:
            return
        settings = read_settings()
        if settings is not None:
            try:
                count = sync(*settings)
            except OSError as e:
                raise ProductImageConfigError(f'Không tải được model từ Drive ({DRIVE_FOLDER_ENV}): {e}')
            logger.info('[ProductImage] model từ Drive: tải %d file vào %s', count, settings[1])
        _synced = True


def start_background_sync():
    """Tải trước lúc gunicorn khởi động để lượt gọi đầu không phải chờ lâu."""
    if 'gunicorn' not in os.path.basename(sys.argv[0]) or not os.environ.get(DRIVE_FOLDER_ENV):
        return

    def run():
        try:
            ensure_models()
        except Exception as e:
            logger.error('[ProductImage] tải model từ Drive hỏng: %s', e)

    threading.Thread(target=run, daemon=True, name='product_image_models').start()
