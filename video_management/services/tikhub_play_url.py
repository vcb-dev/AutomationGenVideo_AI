"""Lấy LINK PHÁT trực tiếp (file mp4) của một video, để xem ngay trên web hệ thống.

Dùng cho 4 nền tảng: Douyin, Kuaishou, Xiaohongshu và TikTok. Bốn nền tảng còn lại
(YouTube, Bilibili, Facebook, Instagram) có mã nhúng chính chủ dùng được, FE nhúng iframe
là xong: không tốn băng thông, không hết hạn, không phải gọi API.

Đã thử thật từng nền tảng, bốn điều bắt buộc phải biết:

  1. Douyin CHẶN THEO REFERER. Gọi từ trình duyệt ở tên miền mình → 403; gọi từ server không
     kèm referer → 206. Nên bắt buộc phải phát qua trung gian ở BE, không thể để thẻ <video>
     trỏ thẳng vào CDN.
  2. Xiaohongshu trả link `http://` (không phải https). Trang chạy HTTPS mà nhúng http là bị
     trình duyệt chặn "mixed content" → cũng phải qua trung gian.
  3. Link CÓ HẠN. Douyin nhúng mốc hết hạn ngay trong đường dẫn (giải mã ra chỉ còn ~3 giờ).
     BE phải cache ngắn hơn hạn đó và tự xin link mới, đừng lưu vào DB.

  4. TikTok tuy CÓ mã nhúng nhưng đã đo: `tiktok.com/embed/v2/<id>` trả 503 liên tục (3/3
     lần). Nên TikTok cũng phải phát qua trung gian; link lấy qua TikHub thì tải bình thường.

Xiaohongshu trả nhiều định dạng (h264/h265/h266/av1) — PHẢI lấy h264, các định dạng kia
Chrome không phát được.
"""

import logging
from typing import Optional
from django.conf import settings

from .tikhub_cache import goi_co_dem

logger = logging.getLogger(__name__)

TIMEOUT = 25

# TikHub tra 402 khi tai khoan het tien. Rat dang phan biet voi "khong tim thay video":
# het tien thi CA BON nen tang chet cung luc va cach chua la nap tien, khong phai sua code.
# Da gap that: moi video deu 404 "khong lay duoc link phat", nhin vao khong ai doan ra.
HET_TIEN = '__HET_TIEN__'

PLAY_URL_ENDPOINTS = {
    'douyin':      {'path': '/api/v1/douyin/web/fetch_one_video',               'param': 'aweme_id',   'arg': 'id'},
    # TikTok CO ma nhung chinh chu, NHUNG da do thuc te: tiktok.com/embed/v2/<id> tra 503
    # lien tuc (ho chan). Nen TikTok cung phai phat qua trung gian nhu 3 nen tang TQ.
    'tiktok':      {'path': '/api/v1/tiktok/app/v3/fetch_one_video',            'param': 'aweme_id',   'arg': 'id'},
    'kuaishou':    {'path': '/api/v1/kuaishou/web/fetch_one_video',             'param': 'share_text', 'arg': 'url'},
    'xiaohongshu': {'path': '/api/v1/xiaohongshu/app_v2/get_video_note_detail', 'param': 'note_id',    'arg': 'id'},
}

SUPPORTED_PLATFORMS = sorted(PLAY_URL_ENDPOINTS.keys())

# Năm nền tảng này KHÔNG đi qua đây — FE nhúng iframe chính chủ.
EMBED_PLATFORMS = ['youtube', 'bilibili', 'facebook', 'instagram']


def _tikhub_base() -> str:
    return getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')


def _first_http(value) -> str:
    """Chuỗi link đầu tiên tìm được, dù nằm trong list hay dict."""
    if isinstance(value, str):
        return value if value.startswith('http') else ''
    if isinstance(value, list):
        for item in value:
            got = _first_http(item)
            if got:
                return got
        return ''
    if isinstance(value, dict):
        for key in ('url_list', 'urlList', 'url', 'master_url'):
            got = _first_http(value.get(key))
            if got:
                return got
    return ''


def _pick_douyin(data: dict) -> str:
    node = data.get('aweme_detail') or data
    video = node.get('video') or {}
    # play_addr_h264 ưu tiên hơn: chắc chắn Chrome phát được.
    for key in ('play_addr_h264', 'play_addr', 'download_addr'):
        got = _first_http(video.get(key))
        if got:
            return got
    return ''


def _pick_kuaishou(data) -> str:
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        got = _first_http(item.get('mainMvUrls'))
        if got:
            return got
    return ''


def _pick_xiaohongshu(data: dict) -> str:
    notes = (data or {}).get('data')
    note = notes[0] if isinstance(notes, list) and notes else data
    stream = (((note or {}).get('video_info_v2') or {}).get('media') or {}).get('stream') or {}
    # CHỈ h264 — h265/h266/av1 Chrome không phát được.
    for entry in stream.get('h264') or []:
        got = _first_http(entry.get('master_url')) or _first_http(entry.get('backup_urls'))
        if got:
            return got
    return ''


PICKERS = {
    'douyin': _pick_douyin,
    'tiktok': _pick_douyin,   # TikTok dung cung cau truc aweme voi Douyin
    'kuaishou': _pick_kuaishou,
    'xiaohongshu': _pick_xiaohongshu,
}


def fetch_play_url(
    platform: str, video_id: str = '', video_url: str = '', lam_moi: bool = False
) -> Optional[str]:
    """Trả link mp4 phát được, hoặc None. Không ném lỗi ra ngoài."""
    platform = (platform or '').lower().strip()
    config = PLAY_URL_ENDPOINTS.get(platform)
    if not config:
        return None

    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        logger.warning('[PLAY-URL] TIKHUB_API_KEY chua cau hinh')
        return None

    arg_value = video_url if config['arg'] == 'url' else video_id
    if not arg_value:
        return None

    # Đi qua bộ đệm dùng chung: tikhub_video_detail.py gọi ĐÚNG endpoint này cho 4 nền tảng,
    # gọi riêng là trả tiền hai lần cho cùng một hồi đáp. Xem tikhub_cache.py.
    resp = goi_co_dem(
        _tikhub_base(), config['path'], {config['param']: arg_value}, api_key,
        timeout=TIMEOUT, lam_moi=lam_moi,
    )
    if resp is None:
        return None

    if resp.status_code == 402:
        logger.error(
            f'[PLAY-URL] {platform} HTTP 402 — TAI KHOAN TIKHUB HET TIEN. '
            'Nap tai https://user.tikhub.io/users/add_credit'
        )
        return HET_TIEN
    if resp.status_code != 200:
        logger.error(f'[PLAY-URL] {platform} HTTP {resp.status_code}')
        return None

    try:
        body = resp.json()
    except ValueError:
        return None
    if body.get('code') != 200:
        logger.error(f'[PLAY-URL] {platform} API code={body.get("code")}')
        return None

    try:
        url = PICKERS[platform](body.get('data'))
    except Exception as e:  # noqa: BLE001 — cấu trúc TikHub đổi thì trả None, đừng vỡ luồng
        logger.exception(f'[PLAY-URL] {platform} boc link loi: {e}')
        return None

    if not url:
        logger.error(f'[PLAY-URL] {platform} khong tim thay link phat')
        return None

    logger.info(f'[PLAY-URL] {platform} id={video_id or arg_value[:30]} -> {url[:70]}')
    return url
