"""Nguồn tải video qua TikHub — đường DỰ PHÒNG khi trình tải miễn phí thất bại.

Dùng cho luồng video → kịch bản (video_script_pipeline). Mỗi lượt gọi TikHub là một lượt TÍNH
PHÍ, nên chỉ gọi khi đường miễn phí đã hỏng, và đi qua bộ đệm chung (tikhub_cache.goi_co_dem).

  douyin / tiktok / kuaishou / xiaohongshu → tikhub_play_url.fetch_play_url (có sẵn): 1 file mp4
  instagram  → /api/v1/instagram/v1/fetch_post_by_url: trường video_url
  bilibili   → fetch_one_video (lấy cid) + /api/v1/bilibili/web/fetch_video_playurl;
               hình và tiếng tách rời → ffmpeg ghép; CDN bắt buộc Referer bilibili.com
  reddit     → /api/v1/reddit/app/fetch_post_details (post_id dạng t3_xxx): luồng HLS có tiếng
               → ffmpeg; không có HLS thì lấy file mp4 dự phòng (có thể thiếu tiếng)

Facebook: TikHub không hỗ trợ (xem tikhub_video_detail.py) → không có dự phòng.
YouTube: KHÔNG có dự phòng. get_video_streams_v2 trả link googlevideo gắn cứng IP máy chủ TikHub
(tham số ip=..., nằm trong sparams) → tải từ máy mình luôn 403 (đo 2026-09-27). Gọi là mất tiền vô ích.

Cấu trúc hồi đáp instagram/bilibili/reddit đã đối chiếu với hồi đáp thật (2026-09-27: cả ba tải
được, có tiếng). Vẫn bóc phòng thủ — dò theo tên trường ở mọi độ sâu — vì TikHub hay đổi vỏ.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from django.conf import settings

from .tikhub_cache import goi_co_dem
from .tikhub_play_url import HET_TIEN, fetch_play_url

logger = logging.getLogger(__name__)

TIMEOUT = 25
BILIBILI_REFERER = 'https://www.bilibili.com/'
PLAY_URL_PLATFORMS = {'douyin', 'tiktok', 'kuaishou', 'xiaohongshu'}
SUPPORTED_PLATFORMS = PLAY_URL_PLATFORMS | {'instagram', 'bilibili', 'reddit'}


@dataclass
class DownloadSource:
    """Một hoặc hai luồng cần tải. `via='file'`: tải thẳng 1 file mp4. `via='ffmpeg'`: HLS hoặc
    ghép luồng hình + luồng tiếng (inputs[0]=hình, inputs[1]=tiếng)."""
    inputs: list
    via: str = 'file'
    headers: dict = field(default_factory=dict)


class TikHubOutOfCredit(Exception):
    pass


def _base() -> str:
    return settings.TIKHUB_API_BASE_URL


def _call(path: str, params: dict) -> Optional[dict]:
    """Gọi TikHub qua bộ đệm. None = lỗi / không có dữ liệu. 402 → TikHubOutOfCredit."""
    api_key = getattr(settings, 'TIKHUB_API_KEY', '')
    if not api_key:
        logger.warning('[DL-SOURCE] TIKHUB_API_KEY chua cau hinh')
        return None
    resp = goi_co_dem(_base(), path, params, api_key, timeout=TIMEOUT)
    if resp is None:
        return None
    if resp.status_code == 402:
        raise TikHubOutOfCredit()
    if resp.status_code != 200:
        logger.error(f'[DL-SOURCE] {path} HTTP {resp.status_code}')
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if body.get('code') not in (200, None):
        logger.error(f'[DL-SOURCE] {path} code={body.get("code")}')
        return None
    return body.get('data')


def _walk(node) -> Iterable:
    """Duyệt mọi (khoá, giá trị) ở mọi độ sâu."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                yield k, v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(x for x in cur if isinstance(x, (dict, list)))


def _find_url(node, keys: Iterable[str], must_contain: str = '') -> str:
    wanted = {k.lower() for k in keys}
    for k, v in _walk(node):
        if str(k).lower() not in wanted:
            continue
        candidates = v if isinstance(v, list) else [v]
        for c in candidates:
            if isinstance(c, dict):
                c = c.get('url') or c.get('src') or ''
            if isinstance(c, str) and c.startswith('http') and must_contain in c:
                return c
    return ''


def _find_value(node, keys: Iterable[str]):
    wanted = {k.lower() for k in keys}
    for k, v in _walk(node):
        if str(k).lower() in wanted and v not in (None, '', [], {}):
            return v
    return None


# ─────────────────────────────── chung ───────────────────────────────

def _height(fmt: dict) -> int:
    for key in ('height', 'h'):
        try:
            if fmt.get(key):
                return int(fmt[key])
        except (TypeError, ValueError):
            pass
    label = str(fmt.get('quality_label') or fmt.get('qualityLabel') or '')
    m = re.match(r'(\d{3,4})p', label)
    return int(m.group(1)) if m else 0


# ─────────────────────────────── Instagram ───────────────────────────────

def pick_instagram(data) -> Optional[DownloadSource]:
    url = _find_url(data, ('video_url', 'video_versions', 'videoUrl'))
    return DownloadSource([url]) if url else None


# ─────────────────────────────── Bilibili ───────────────────────────────

def _bili_base_url(item: dict) -> str:
    return str(item.get('baseUrl') or item.get('base_url') or item.get('url') or '')


def pick_bilibili_playurl(data) -> Optional[DownloadSource]:
    headers = {'Referer': BILIBILI_REFERER}
    durl = _find_value(data, ('durl',))
    if isinstance(durl, list) and durl and isinstance(durl[0], dict) and durl[0].get('url'):
        return DownloadSource([durl[0]['url']], via='ffmpeg', headers=headers)
    dash = _find_value(data, ('dash',))
    if not isinstance(dash, dict):
        return None
    videos = [v for v in (dash.get('video') or []) if isinstance(v, dict) and _bili_base_url(v)]
    audios = [a for a in (dash.get('audio') or []) if isinstance(a, dict) and _bili_base_url(a)]
    if not videos:
        return None
    # Ưu tiên H.264 (codecid 7) ≤480p cho nhẹ; không có thì bản thấp nhất.
    avc = [v for v in videos if v.get('codecid') == 7] or videos
    low = sorted((v for v in avc if 0 < _height(v) <= 480), key=_height, reverse=True)
    video = low[0] if low else sorted(avc, key=lambda v: _height(v) or 9999)[0]
    inputs = [_bili_base_url(video)]
    if audios:
        inputs.append(_bili_base_url(sorted(audios, key=lambda a: a.get('bandwidth') or 0)[0]))
    return DownloadSource(inputs, via='ffmpeg', headers=headers)


def _bilibili(video_id: str, video_url: str) -> Optional[DownloadSource]:
    bv = video_id or _match(r'/video/(BV[\w]{8,})', video_url)
    if not bv:
        return None
    info = _call('/api/v1/bilibili/web/fetch_one_video', {'bv_id': bv})
    cid = _find_value(info, ('cid',)) if info else None
    if not cid:
        return None
    play = _call('/api/v1/bilibili/web/fetch_video_playurl', {'bv_id': bv, 'cid': str(cid)})
    return pick_bilibili_playurl(play) if play else None


# ─────────────────────────────── Reddit ───────────────────────────────

def pick_reddit(data) -> Optional[DownloadSource]:
    """Ưu tiên luồng HLS (.m3u8, có cả tiếng); không có thì file mp4 dự phòng của v.redd.it
    (DASH_xxx.mp4 — chỉ có hình, Gemini vẫn xem được hình)."""
    hls = _find_url(data, ('hls_url', 'hlsUrl', 'hls'), must_contain='.m3u8')
    if hls:
        return DownloadSource([hls], via='ffmpeg')
    mp4 = _find_url(data, ('fallback_url', 'fallbackUrl', 'url'), must_contain='v.redd.it')
    return DownloadSource([mp4]) if mp4 else None


def reddit_post_id(video_id: str, video_url: str) -> str:
    pid = video_id or _match(r'/comments/([a-z0-9]{4,10})', video_url) \
        or _match(r'//(?:www\.)?redd\.it/([a-z0-9]{4,10})', video_url)
    return pid.lower()


def _match(pattern: str, text: str) -> str:
    m = re.search(pattern, text or '', re.I)
    return m.group(1) if m else ''


# ─────────────────────────────── entry ───────────────────────────────

def fetch_download_source(platform: str, video_id: str = '', video_url: str = '') -> Optional[DownloadSource]:
    """Nguồn tải cho video, None nếu không lấy được. Ném TikHubOutOfCredit khi hết tiền."""
    platform = (platform or '').lower()
    if platform in PLAY_URL_PLATFORMS:
        url = fetch_play_url(platform, video_id=video_id, video_url=video_url)
        if url == HET_TIEN:
            raise TikHubOutOfCredit()
        return DownloadSource([url]) if url else None
    try:
        if platform == 'instagram':
            data = _call('/api/v1/instagram/v1/fetch_post_by_url', {'post_url': video_url})
            return pick_instagram(data) if data else None
        if platform == 'bilibili':
            return _bilibili(video_id, video_url)
        if platform == 'reddit':
            pid = reddit_post_id(video_id, video_url)
            if not pid:
                return None
            data = _call('/api/v1/reddit/app/fetch_post_details', {'post_id': f't3_{pid}'})
            return pick_reddit(data) if data else None
    except TikHubOutOfCredit:
        raise
    except Exception as e:  # noqa: BLE001 — cấu trúc TikHub đổi thì trả None, đừng vỡ luồng
        logger.exception(f'[DL-SOURCE] {platform} boc nguon tai loi: {e}')
    return None
