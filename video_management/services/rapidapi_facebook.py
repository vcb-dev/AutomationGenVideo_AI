"""RapidAPI Facebook scraper — fetch + parse only (no DB access).

BE (Prisma) sở hữu toàn bộ việc ghi scraper_fanpages/scraper_facebook_reels/
scraper_fanpage_metrics_history. AI chỉ gọi RapidAPI + parse dữ liệu, trả dict
thô cho BE tự lưu.
"""

import logging
import re
import time
import requests
from datetime import datetime, timezone as tz_dt
from typing import Optional
from django.conf import settings

logger = logging.getLogger(__name__)


def _rapidapi_host() -> str:
    return getattr(settings, 'RAPIDAPI_FACEBOOK_HOST', 'facebook-scraper-api4.p.rapidapi.com')


def _rapidapi_profile_url() -> str:
    return f"https://{_rapidapi_host()}/get_facebook_pages_details_from_link"


def _rapidapi_reels_url() -> str:
    return f"https://{_rapidapi_host()}/get_facebook_reels_details"


def _get_api_key() -> str:
    """Key RapidAPI duy nhất. Hết hạn mức thì thay trực tiếp trên Railway.

    Bỏ cơ chế xoay vòng nhiều key: vận hành thực tế đơn giản hơn khi chỉ có một key, và
    xoay vòng che mất thời điểm cần đi thay — log báo "chuyển sang key tiếp theo" nghe như
    đã tự xử lý xong.

    Biến môi trường cũ có thể còn nhiều key nối bằng dấu phẩy. Lấy nguyên chuỗi sẽ gửi
    header "key1,key2,key3" và hỏng mọi request với lỗi 401/403 chung chung rất khó lần ra,
    nên lấy key đầu và cảnh báo để người trực biết mà dọn lại biến môi trường.
    """
    raw = (getattr(settings, 'RAPIDAPI_FACEBOOK_KEY', '') or '').strip()
    if ',' not in raw:
        return raw

    first = raw.split(',')[0].strip()
    logger.warning(
        "[FB-RAPIDAPI] RAPIDAPI_FACEBOOK_KEY đang chứa nhiều key nối bằng dấu phẩy. "
        "Hệ thống chỉ dùng key đầu tiên — hãy sửa lại biến môi trường trên Railway để "
        "chỉ còn một key."
    )
    return first


def _is_soft_error_body(resp: requests.Response) -> bool:
    """Phản hồi mã 2xx nhưng thân báo lỗi — nhà cung cấp trả HTTP 209 kiểu này.

    Ví dụ thật gặp phải: {"success": false, "message": "Access conflict: Another user
    with similar credentials is already using the API..."} — họ chặn khi nhiều key cùng
    tài khoản gọi sát nhau. Mã 209 lọt qua raise_for_status() nên phải tự nhận diện.
    """
    if resp.status_code == 200:
        return False
    try:
        body = resp.json()
    except Exception:
        return False
    return isinstance(body, dict) and body.get('success') is False


def _request_with_key(url: str, params: dict, timeout: int = 30) -> Optional[requests.Response]:
    """Gọi RapidAPI bằng key duy nhất. Trả None kèm log nêu rõ nguyên nhân khi hỏng.

    Không thử lại: chỉ có một key nên thử lại chỉ tốn thêm lượt gọi mà kết quả không đổi.
    Đổi lại, log phải nói rõ PHẢI LÀM GÌ — đây là điểm dừng chứ không còn key dự phòng.
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("[FB-RAPIDAPI] RAPIDAPI_FACEBOOK_KEY chưa được cấu hình.")
        return None

    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": _rapidapi_host(),
        "Content-Type": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)

        if resp.status_code == 429 or "exceeded the MONTHLY quota" in resp.text:
            logger.error(
                "[FB-RAPIDAPI] Key đã HẾT HẠN MỨC tháng. Vào Railway thay "
                "RAPIDAPI_FACEBOOK_KEY bằng key mới rồi restart service."
            )
            return None

        if resp.status_code == 403 and "not subscribed" in resp.text:
            logger.error(
                "[FB-RAPIDAPI] Key CHƯA SUBSCRIBE API này. Vào RapidAPI subscribe cho đúng "
                f"host '{_rapidapi_host()}', hoặc thay key khác trên Railway."
            )
            return None

        # api4 báo lỗi bằng HTTP 209 + {"success": false} — 2xx nên raise_for_status() bỏ
        # qua, và caller chỉ thấy payload sai kiểu rồi trả None mà không rõ vì sao.
        if _is_soft_error_body(resp):
            logger.error(
                f"[FB-RAPIDAPI] Nhà cung cấp từ chối (HTTP {resp.status_code}): "
                f"{resp.text[:160]}"
            )
            return None

        resp.raise_for_status()
        return resp
    except Exception as e:
        logger.error(f"[FB-RAPIDAPI] Gọi API thất bại: {e}")
        return None


def _is_scraper3(host: str) -> bool:
    return 'facebook-scraper3' in host


def _to_int(value, default: int = 0) -> int:
    """Ép kiểu số an toàn. Nhà cung cấp có thể trả '1.2M', '1,234' hoặc None."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or '').strip().replace(',', '')
    if not text:
        return default
    multiplier = 1
    if text[-1].upper() in ('K', 'M', 'B'):
        multiplier = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}[text[-1].upper()]
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return default


# ── Profile detail ────────────────────────────────────────────────────────────

def fetch_page_profile(page_url: str) -> Optional[dict]:
    """Gọi profile detail API, trả về dict page hoặc None nếu lỗi."""
    host = _rapidapi_host()
    if _is_scraper3(host):
        url = f"https://{host}/page/details"
        params = {"url": page_url}
        resp = _request_with_key(url, params=params, timeout=30)
        if not resp:
            return None
        try:
            data = resp.json()
            res = data.get("results")
            if res and isinstance(res, dict):
                return {
                    "title": res.get("name"),
                    "ad_page_id": str(res.get("page_id") or ""),
                    "url": res.get("url") or page_url,
                    "image": res.get("image") or "",
                    "followers_count": _to_int(res.get("followers")),
                    "reels_page_id": res.get("reels_page_id") or "",
                }
            return None
        except Exception as e:
            logger.warning(f"[FB-PROFILE-SCRAPER3] {page_url}: {e}")
            return None

    url = f"https://{host}/get_facebook_pages_details_from_link"
    params = {
        "link": page_url,
        "exact_followers_count": "true",
        "show_verified_badge": "false",
        "proxy_country": "us",
        "page_section": "default",
    }
    resp = _request_with_key(url, params=params, timeout=30)
    if not resp:
        return None

    try:
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception as e:
        logger.warning(f"[FB-PROFILE-API4] {page_url}: {e}")
        return None


# ── Reels fetch (có pagination) ───────────────────────────────────────────────

def _fetch_reels_page(page_url: str, cursor: Optional[str] = None, reels_page_id: Optional[str] = None) -> tuple:
    """Fetch 1 trang reels. Returns (reels_list, next_cursor, has_next)."""
    host = _rapidapi_host()
    if _is_scraper3(host):
        # /page/reels CHỈ nhận reels_page_id — một token base64 do /page/details cấp,
        # không phải page_id số. Thiếu token thì gửi 'url' sẽ ăn 400 và bị thử lại lần
        # lượt trên từng key, đốt quota mà chắc chắn không ra reels. Page không có tab
        # Reels thì /page/details trả reels_page_id = None, đây là ngõ cụt thật sự.
        if not reels_page_id:
            logger.warning(
                f"[FB-REELS-SCRAPER3] {page_url}: /page/details không trả reels_page_id "
                f"— page này không có Reels hoặc nhà cung cấp không đọc được."
            )
            return [], None, False

        url = f"https://{host}/page/reels"
        params = {"reels_page_id": reels_page_id}
        if cursor:
            params["cursor"] = cursor

        resp = _request_with_key(url, params=params, timeout=30)
        if not resp:
            return [], None, False
        try:
            data = resp.json()
            reels = data.get("results", [])
            next_cursor = data.get("cursor")
            has_next = bool(next_cursor)
            return reels, next_cursor, has_next
        except Exception as e:
            logger.warning(f"[FB-REELS-SCRAPER3] {page_url}: {e}")
            return [], None, False

    url = f"https://{host}/get_facebook_reels_details"
    params = {"link": page_url}
    if cursor:
        params["cursor"] = cursor

    resp = _request_with_key(url, params=params, timeout=30)
    if not resp:
        return [], None, False

    try:
        inner = resp.json().get("data", {})
        reels = inner.get("reels", [])
        page_info = inner.get("page_info", {})
        has_next = bool(page_info.get("has_next"))
        next_cursor = page_info.get("end_cursor") if has_next else None
        return reels, next_cursor, has_next
    except Exception as e:
        logger.warning(f"[FB-REELS-API4] {page_url}: {e}")
        return [], None, False


def fetch_reels_only(
    page_url: str,
    num_of_posts: int = 30,
    exclude_post_ids: Optional[list] = None,
    start_date: str = '',
    profile: Optional[dict] = None,
) -> list:
    """Chỉ fetch reels. Returns reels_list (raw, chưa parse).

    `profile`: kết quả fetch_page_profile caller đã có sẵn. Truyền vào để tránh gọi
    lại profile API — mỗi lần gọi thừa là một lần đốt quota RapidAPI.
    """
    exclude_set = set(str(x) for x in (exclude_post_ids or []))
    start_dt: Optional[datetime] = None
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=tz_dt.utc)
        except ValueError:
            pass

    all_reels: list = []
    cursor: Optional[str] = None
    reels_page_id: Optional[str] = None

    if _is_scraper3(_rapidapi_host()):
        prof = profile if profile is not None else fetch_page_profile(page_url)
        reels_page_id = (prof or {}).get("reels_page_id")

    while len(all_reels) < num_of_posts:
        batch, next_cursor, has_next = _fetch_reels_page(page_url, cursor, reels_page_id)
        if not batch:
            break

        reached_old = False
        for reel in batch:
            post_id = str(reel.get('post_id', '') or '')
            if not post_id or post_id in exclude_set:
                continue
            if start_dt:
                ts = reel.get('timestamp') or 0
                reel_dt = datetime.fromtimestamp(ts, tz=tz_dt.utc) if ts else None
                if reel_dt and reel_dt < start_dt:
                    reached_old = True
                    break
            all_reels.append(reel)
            if len(all_reels) >= num_of_posts:
                break

        if reached_old or not has_next or not next_cursor:
            break
        cursor = next_cursor
        time.sleep(0.5)

    return all_reels


# ── Helper: parse ─────────────────────────────────────────────────────────────

def _extract_handle(url: str) -> str:
    m = re.search(r'facebook\.com/([^/?&#]+)', url or '')
    return m.group(1) if m else ''


def _extract_hashtags(text: str) -> list:
    return [m.lstrip('#') for m in re.findall(r'#\w+', text or '')]


def parse_fanpage_profile(profile: Optional[dict], first_reel: dict) -> Optional[dict]:
    """Parse profile API data + author trong reel đầu tiên thành dict thô (không ghi DB).

    Giá trị ưu tiên: profile API > author trong reel. BE tự quyết định field nào
    ghi đè (chỉ ghi khi có giá trị — khớp hành vi _upsert_fanpage cũ).
    """
    author = (first_reel or {}).get('author') or {}
    delegate = author.get('delegate_page') or {}

    profile_id = str(
        (profile or {}).get('ad_page_id', '')
        or delegate.get('id', '')
        or author.get('id', '')
        or ''
    ).strip()
    if not profile_id:
        return None

    if profile:
        raw_name = profile.get('title') or author.get('name') or ''
        name = re.sub(r'\s*\|.*$', '', raw_name).strip()
        page_url = profile.get('url') or author.get('url') or ''
        avatar_url = (
            profile.get('image')
            or author.get('profile_picture_url')
            or (author.get('displayPicture') or {}).get('uri')
            or ''
        )
        followers_count = _to_int(profile.get('followers_count'))
    else:
        name = author.get('name') or ''
        page_url = author.get('url') or ''
        avatar_url = (
            author.get('profile_picture_url')
            or (author.get('displayPicture') or {}).get('uri')
            or ''
        )
        followers_count = 0

    return {
        'profile_id': profile_id,
        'name': name,
        'page_url': page_url,
        'handle': _extract_handle(page_url),
        'avatar_url': avatar_url,
        'is_verified': author.get('is_verified'),
        'followers_count': followers_count,
    }


def parse_facebook_reels(reels_list: list) -> list:
    """Parse reels JSON (RapidAPI format) thành list dict thô (không ghi DB)."""
    parsed = []

    for reel in reels_list:
        post_id = str(reel.get('post_id') or '')
        # video_id dùng làm shortcode (unique per reel)
        shortcode = str(reel.get('video_id') or post_id)
        if not post_id or not shortcode:
            continue

        ts = reel.get('timestamp') or 0
        date_posted = (
            datetime.fromtimestamp(ts, tz=tz_dt.utc) if ts else datetime.now(tz=tz_dt.utc)
        )

        description = reel.get('description') or ''
        video_files = reel.get('video_files') or {}
        video_url = (
            reel.get('browser_native_hd_url')
            or reel.get('browser_native_sd_url')
            or video_files.get('video_hd_file')
            or video_files.get('video_sd_file')
            or ''
        )

        parsed.append({
            'post_id': post_id,
            'shortcode': shortcode,
            'url': reel.get('url') or '',
            'content': description,
            'hashtags': _extract_hashtags(description),
            'video_url': video_url,
            'thumbnail_url': reel.get('thumbnail_uri') or '',
            'duration_seconds': reel.get('length_in_seconds') or reel.get('length_in_second'),
            'has_audio': True,  # API không trả về, mặc định True
            'date_posted': date_posted.isoformat(),
            'views_count': _to_int(reel.get('play_count')),
            'likes_count': _to_int(reel.get('reactions_count')),
            'comments_count': _to_int(reel.get('comments_count')),
            'shares_count': _to_int(reel.get('reshare_count')),
        })

    return parsed
