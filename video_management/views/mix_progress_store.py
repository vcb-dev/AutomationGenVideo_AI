"""
Mix Progress Store — Redis-backed with in-memory fallback.

Thay thế _mix_progress dict + threading.Lock() bằng Redis để:
1. Progress không bị mất khi server restart
2. Nhiều Gunicorn worker chia sẻ được state
3. TTL tự động dọn dẹp job cũ (4 giờ)

Fallback: nếu Redis không kết nối được → dùng RAM dict như cũ
(đảm bảo hệ thống không bị crash hoàn toàn khi Redis down)
"""

import json
import logging
import threading
import time
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# TTL cho mỗi progress entry trong Redis (4 giờ)
PROGRESS_TTL_SECONDS = 4 * 3600

# Prefix key trong Redis
REDIS_KEY_PREFIX = "mix_progress:"

# ─── Lazy Redis connection ──────────────────────────────────────────────────
_redis_client = None
_redis_lock = threading.Lock()
_redis_last_attempt = 0.0
_REDIS_RETRY_COOLDOWN = 30  # giây — tránh retry connect (và ăn socket_connect_timeout) ở MỌI lần gọi khi Redis đang down


def _get_redis():
    """
    Lazy-init Redis client. Returns None nếu không kết nối được.
    Khi Redis down, chỉ thử kết nối lại mỗi _REDIS_RETRY_COOLDOWN giây thay vì mỗi lần gọi —
    nếu không, các job gọi progress_update() liên tục (vd tải video) sẽ bị cộng dồn độ trễ
    socket_connect_timeout ở từng lần cập nhật, làm chậm hẳn cả request đang xử lý.
    """
    global _redis_client, _redis_last_attempt
    if _redis_client is not None:
        return _redis_client
    now = time.time()
    if now - _redis_last_attempt < _REDIS_RETRY_COOLDOWN:
        return None
    with _redis_lock:
        if _redis_client is not None:
            return _redis_client
        now = time.time()
        if now - _redis_last_attempt < _REDIS_RETRY_COOLDOWN:
            return None
        _redis_last_attempt = now
        try:
            import redis
            from django.conf import settings
            url = getattr(settings, 'CELERY_BROKER_URL', 'redis://redis:6379/0')
            client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
            client.ping()  # test connection
            _redis_client = client
            logger.info(f"✅ Mix Progress Store: Redis connected ({url})")
        except Exception as e:
            logger.warning(f"⚠️ Mix Progress Store: Redis not available ({e}). Falling back to in-memory dict for {_REDIS_RETRY_COOLDOWN}s.")
            _redis_client = None
    return _redis_client


# ─── In-memory fallback (with TTL eviction) ─────────────────────────────────
# Values stored as (data, expires_at) tuples to prevent unbounded memory growth
_mem_progress: Dict[str, Tuple[Dict, float]] = {}
_mem_lock = threading.Lock()


def _mem_get(progress_id: str) -> Optional[Dict]:
    """Read from mem fallback, evicting expired entry if found."""
    entry = _mem_progress.get(progress_id)
    if entry is None:
        return None
    data, expires_at = entry
    if time.time() > expires_at:
        _mem_progress.pop(progress_id, None)
        return None
    return data


def _mem_set(progress_id: str, data: Dict) -> None:
    """Write to mem fallback with TTL."""
    _mem_progress[progress_id] = (data, time.time() + PROGRESS_TTL_SECONDS)


# ─── Public API ─────────────────────────────────────────────────────────────

def progress_set(progress_id: str, data: Dict[str, Any]) -> None:
    """Lưu toàn bộ progress data (ghi đè)."""
    r = _get_redis()
    if r:
        try:
            r.setex(
                f"{REDIS_KEY_PREFIX}{progress_id}",
                PROGRESS_TTL_SECONDS,
                json.dumps(data, default=str)
            )
            return
        except Exception as e:
            logger.warning(f"Redis set failed ({e}), using memory fallback")

    with _mem_lock:
        _mem_set(progress_id, data)


def progress_update(progress_id: str, updates: Dict[str, Any]) -> None:
    """Cập nhật một số field trong progress (merge, không ghi đè toàn bộ)."""
    r = _get_redis()
    if r:
        try:
            key = f"{REDIS_KEY_PREFIX}{progress_id}"
            raw = r.get(key)
            current = json.loads(raw) if raw else {}
            current.update(updates)
            r.setex(key, PROGRESS_TTL_SECONDS, json.dumps(current, default=str))
            return
        except Exception as e:
            logger.warning(f"Redis update failed ({e}), using memory fallback")

    with _mem_lock:
        current = _mem_get(progress_id) or {}
        current.update(updates)
        _mem_set(progress_id, current)


def progress_get(progress_id: str) -> Optional[Dict[str, Any]]:
    """Đọc progress data. Trả về None nếu không tồn tại."""
    r = _get_redis()
    if r:
        try:
            raw = r.get(f"{REDIS_KEY_PREFIX}{progress_id}")
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.warning(f"Redis get failed ({e}), using memory fallback")

    with _mem_lock:
        return _mem_get(progress_id)


def progress_exists(progress_id: str) -> bool:
    """Kiểm tra progress_id có tồn tại không."""
    return progress_get(progress_id) is not None


def progress_delete(progress_id: str) -> None:
    """Xóa progress entry (dùng sau khi cleanup xong)."""
    r = _get_redis()
    if r:
        try:
            r.delete(f"{REDIS_KEY_PREFIX}{progress_id}")
            return
        except Exception as e:
            logger.warning(f"Redis delete failed ({e}), using memory fallback")

    with _mem_lock:
        _mem_progress.pop(progress_id, None)  # direct pop — no TTL concern on delete


def progress_get_field(progress_id: str, field: str, default=None):
    """Đọc 1 field cụ thể."""
    data = progress_get(progress_id)
    if data is None:
        return default
    return data.get(field, default)
