"""Bộ đệm dùng chung cho các lượt gọi TikHub — mỗi lượt gọi là một lượt TÍNH PHÍ.

Vì sao phải có tệp này:

`tikhub_play_url.py` và `tikhub_video_detail.py` gọi **CÙNG MỘT endpoint** của TikHub cho
bốn nền tảng, chỉ khác chỗ bóc trường nào ra khỏi hồi đáp:

    douyin       → /api/v1/douyin/web/fetch_one_video
    tiktok       → /api/v1/tiktok/app/v3/fetch_one_video
    kuaishou     → /api/v1/kuaishou/web/fetch_one_video
    xiaohongshu  → /api/v1/xiaohongshu/app_v2/get_video_note_detail

Trước đây hai bên gọi riêng, không ai biết ai, nên một video vừa được đề xuất (lấy chi tiết)
vừa được xem (lấy link phát) là **trả tiền hai lần cho đúng một hồi đáp**.

Số liệu thật từ hoá đơn ngày 2026-07-30: riêng ba endpoint `fetch_one_video` chiếm 38/146
lượt trong ngày (26%).

Hạn đệm 1 giờ là mức thoả hiệp có chủ ý:
  - Link phát của Douyin hết hạn khoảng 3 giờ → 1 giờ vẫn còn dư an toàn.
  - Các chỉ số (view/like/comment) chỉ cũ tối đa 1 giờ, đủ tươi cho việc duyệt video.
"""

import hashlib
import json
import logging
from typing import Optional

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: Ngắn hơn hạn sống của link phát (~3 giờ) để link lấy từ bộ đệm chắc chắn còn dùng được.
TTL_MAC_DINH = 3600


def _khoa(path: str, params: dict) -> str:
    """Khoá phải gồm CẢ tham số — cùng endpoint nhưng khác video là hai hồi đáp khác nhau."""
    goi = json.dumps(params, sort_keys=True, ensure_ascii=False)
    bam = hashlib.sha1(f'{path}|{goi}'.encode('utf-8')).hexdigest()[:24]
    return f'tikhub:{bam}'


def _thanh_cong(body: dict) -> bool:
    """TikHub trả HTTP 200 nhưng gắn mã lỗi TRONG THÂN khi video đã xoá / bị khoá / sai mã.

    Chỉ nhìn `status_code == 200` là đệm luôn cả những hồi đáp hỏng đó suốt một tiếng —
    đúng cái bẫy mà chú thích của chính tệp này cảnh báo nhưng lại chỉ chặn ở tầng HTTP.
    Các tệp khác trong thư mục này đã coi 200/0/None là thành công, giữ nguyên quy ước đó.
    """
    if not isinstance(body, dict):
        return False
    return body.get('code') in (200, 0, None)


def goi_co_dem(
    base_url: str,
    path: str,
    params: dict,
    api_key: str,
    timeout: int = 25,
    ttl: int = TTL_MAC_DINH,
    lam_moi: bool = False,
) -> Optional[requests.Response]:
    """Gọi TikHub nhưng dùng lại hồi đáp cũ nếu vẫn còn trong bộ đệm.

    Trả về đối tượng Response thật (khi phải gọi mới) hoặc một vật thay thế mang đúng
    `status_code` và `json()` (khi lấy từ bộ đệm) — bên gọi không cần biết khác biệt.

    CHỈ đệm hồi đáp THÀNH CÔNG. Đệm cả lỗi thì một lần TikHub trục trặc sẽ làm video chết
    suốt cả tiếng dù lát sau đã bình thường trở lại — đúng cái bẫy đã mắc một lần ở tầng BE.
    """
    khoa = _khoa(path, params)
    # lam_moi=True: BẮT BUỘC gọi mới. Dùng khi bên gọi vừa phát hiện dữ liệu cũ đã hỏng —
    # cụ thể là lúc BE gặp 403 vì link phát hết hạn rồi xin lại. Không có cửa này thì bộ đệm
    # trả đúng cái link vừa hỏng đó, vòng thử-lại của BE thành vô nghĩa.
    da_co = None if lam_moi else cache.get(khoa)
    if da_co is not None:
        logger.info(f'[TIKHUB-CACHE] dung lai hoi dap cu, khong ton luot goi: {path}')
        return _HoiDapTuDem(da_co)

    try:
        resp = requests.get(
            f'{base_url}{path}',
            params=params,
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=timeout,
        )
    except requests.RequestException as e:
        logger.error(f'[TIKHUB-CACHE] {path} loi mang: {e}')
        return None

    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            return resp  # không phải JSON thì không đệm
        if _thanh_cong(body):
            cache.set(khoa, body, ttl)
        else:
            logger.info(f'[TIKHUB-CACHE] khong dem hoi dap loi (code={body.get("code")}): {path}')
    return resp


class _HoiDapTuDem:
    """Vỏ bọc để bên gọi dùng y hệt một Response của requests."""

    status_code = 200
    ok = True

    def __init__(self, body: dict):
        self._body = body

    def json(self) -> dict:
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body, ensure_ascii=False)[:500]
