"""
TikTok Search Suggestion API — Smart Two-Phase Strategy

Phase 1 (instant ~150ms): YouTube Suggest — returns for every partial query
Phase 2 (background, ~6s): Gemini with Google Search Grounding — researches
  real trending/viral content on the internet, builds a 40-item pool.
  Subsequent same-query searches serve from this pool (rotate offset) =
  DIFFERENT suggestions every time, always market/viral-aware.

Pool TTL: 1 hour → auto-refresh with fresh Gemini research.

YouTube/Google cache: Full list cached, each request returns random sample
  → Same query typed again = DIFFERENT suggestions (rotation/shuffle).
"""
import json
import random
import time
import logging
import requests
import threading

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

logger = logging.getLogger(__name__)

# ─── Short-term cache (YouTube results, 5min) ─────────────────────────────────
_yt_cache: dict = {}
_yt_lock = threading.Lock()
YT_CACHE_TTL = 300  # 5 minutes
YT_FETCH_SIZE = 25  # Fetch more for rotation pool (each request = different sample)

# ─── Gemini pool cache (40 suggestions per query, 1 hour TTL) ────────────────
_gemini_pool: dict = {}   # query_lower → { pool: [...], offset: int, ts: float }
_gemini_lock = threading.Lock()
_generating: set = set()  # queries currently being generated
GEMINI_POOL_TTL = 3600    # 1 hour
GEMINI_POOL_SIZE = 40     # how many suggestions Gemini generates per query


# ─── Cache helpers ────────────────────────────────────────────────────────────

def _yt_get(key: str):
    with _yt_lock:
        e = _yt_cache.get(key)
        if e and time.time() - e['ts'] < YT_CACHE_TTL:
            return e['data']
    return None


def _yt_set(key: str, data):
    with _yt_lock:
        if len(_yt_cache) > 500:
            oldest = sorted(_yt_cache.items(), key=lambda x: x[1]['ts'])[:200]
            for k, _ in oldest:
                del _yt_cache[k]
        _yt_cache[key] = {'data': data, 'ts': time.time()}


def _random_sample_from_pool(pool: list, count: int) -> list:
    """Return random sample of `count` items. Same query → different suggestions each time."""
    if not pool or count <= 0:
        return []
    if len(pool) <= count:
        out = list(pool)
        random.shuffle(out)
        return out
    return random.sample(pool, count)


def _gemini_get_next_batch(query_lower: str, count: int):
    """Get next `count` suggestions from Gemini pool, advancing offset."""
    with _gemini_lock:
        entry = _gemini_pool.get(query_lower)
        if not entry:
            return None
        # Check TTL
        if time.time() - entry['ts'] > GEMINI_POOL_TTL:
            del _gemini_pool[query_lower]
            return None
        pool = entry['pool']
        if not pool:
            return None
        offset = entry['offset']
        batch = []
        for i in range(count):
            batch.append(pool[(offset + i) % len(pool)])
        # Advance offset for NEXT call (different suggestions next time)
        entry['offset'] = (offset + count) % len(pool)
        return batch


def _gemini_store_pool(query_lower: str, pool: list):
    with _gemini_lock:
        _gemini_pool[query_lower] = {
            'pool': pool,
            'offset': 0,
            'ts': time.time(),
        }


# ─── Main endpoint ────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([AllowAny])
def tiktok_search_suggest(request):
    """
    Two-phase suggestion strategy:
    1. YouTube Suggest — instant, real market data for current partial query
    2. Gemini pool — if available, serves rotating batches (different each search)
       If not available, triggers background generation for next time.
    """
    query = request.GET.get('q', '').strip()
    count = min(int(request.GET.get('count', 8)), 20)

    if not query:
        return Response({'success': True, 'suggestions': [], 'source': 'none', 'query': ''})

    query_lower = query.lower()

    # ── Check Gemini pool first (rich, viral-aware, rotates per search) ───────
    # Only use Gemini pool for "complete words" (query >= 3 chars, not mid-typing)
    if len(query) >= 3:
        gemini_batch = _gemini_get_next_batch(query_lower, count)
        if gemini_batch:
            logger.info(f"[Suggest] Gemini pool HIT: '{query}' → {len(gemini_batch)} rotated")
            # Trigger background refresh if pool is getting stale (> 50min)
            with _gemini_lock:
                entry = _gemini_pool.get(query_lower)
                if entry and time.time() - entry['ts'] > 3000:
                    _trigger_gemini_background(query, count)
            return Response({
                'success': True,
                'suggestions': gemini_batch,
                'source': 'gemini',
                'query': query,
            })

    # ── YouTube Suggest (always fast, real-time for partial queries) ──────────
    yt_key = f"yt:{query_lower}"
    pool = _yt_get(yt_key)
    if pool:
        logger.info(f"[Suggest] YouTube cache HIT: '{query}'")
    else:
        pool = _fetch_youtube_suggestions(query, YT_FETCH_SIZE)
        if pool:
            _yt_set(yt_key, pool)

    # Fallback to Google if YouTube empty
    if not pool:
        pool = _fetch_google_suggestions(query, YT_FETCH_SIZE)
        source = 'google'
    else:
        source = 'youtube'

    # ── Rotate: each request gets different random sample from pool ───────────
    suggestions = _random_sample_from_pool(pool or [], count)

    # ── Trigger Gemini background generation (if not already running) ─────────
    if len(query) >= 3:
        _trigger_gemini_background(query, count)

    return Response({
        'success': True,
        'suggestions': suggestions,
        'source': source,
        'query': query,
    })


def _trigger_gemini_background(query: str, count: int):
    """Spawn background thread to generate Gemini pool (non-blocking)."""
    query_lower = query.lower()
    with _gemini_lock:
        if query_lower in _generating:
            return
        if query_lower in _gemini_pool:
            entry = _gemini_pool[query_lower]
            # Skip if pool is fresh (< 50 min old)
            if time.time() - entry['ts'] < 3000:
                return
        _generating.add(query_lower)

    def _generate():
        try:
            logger.info(f"[Suggest] Gemini background generating pool for '{query}'...")
            pool = _fetch_gemini_pool(query, GEMINI_POOL_SIZE)
            if pool:
                _gemini_store_pool(query_lower, pool)
                logger.info(f"[Suggest] Gemini pool ready: '{query}' → {len(pool)} suggestions")
        except Exception as e:
            logger.error(f"[Suggest] Gemini pool generation failed: {e}")
        finally:
            with _gemini_lock:
                _generating.discard(query_lower)

    thread = threading.Thread(target=_generate, daemon=True)
    thread.start()


# ─── YouTube Suggest ──────────────────────────────────────────────────────────

def _fetch_youtube_suggestions(query: str, max_items: int = 25) -> list:
    """YouTube autocomplete — video-tuned, ~100ms, no key required. Returns full pool for rotation."""
    try:
        resp = requests.get(
            'https://suggestqueries.google.com/complete/search',
            params={'client': 'firefox', 'ds': 'yt', 'q': query, 'hl': 'vi', 'gl': 'vn', 'oe': 'utf-8'},
            headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'vi-VN,vi;q=0.9'},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 1:
                items = [s for s in data[1] if isinstance(s, str) and s.strip()]
                logger.info(f"[Suggest] YouTube: '{query}' → {len(items)} results")
                return items[:max_items]
    except Exception as e:
        logger.warning(f"[Suggest] YouTube error: {e}")
    return []


# ─── Google Suggest ───────────────────────────────────────────────────────────

def _fetch_google_suggestions(query: str, max_items: int = 25) -> list:
    """Google web autocomplete fallback. Returns full pool for rotation."""
    try:
        resp = requests.get(
            'https://suggestqueries.google.com/complete/search',
            params={'client': 'firefox', 'q': query, 'hl': 'vi', 'gl': 'vn', 'oe': 'utf-8'},
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=2,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 1:
                return [s for s in data[1] if isinstance(s, str) and s.strip()][:max_items]
    except Exception as e:
        logger.warning(f"[Suggest] Google error: {e}")
    return []


# ─── Gemini with Google Search Grounding (pool generation) ───────────────────

def _fetch_gemini_pool(query: str, pool_size: int = 40) -> list:
    """
    Uses Gemini to research the web and generate a rich pool of suggestions.
    With google_search tool enabled: Gemini actually searches for current
    viral/trending content related to the query before generating suggestions.
    Returns pool_size diverse, market-aware suggestions.
    """
    api_key = getattr(settings, 'GEMINI_API_KEY', '')
    if not api_key:
        logger.warning("[Suggest] GEMINI_API_KEY not set — skipping Gemini pool")
        return []

    try:
        model = "gemini-2.0-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        prompt = f"""Bạn là chuyên gia nghiên cứu xu hướng TikTok Việt Nam.

Nhiệm vụ: Tạo ra {pool_size} gợi ý tìm kiếm đa dạng và phong phú liên quan đến "{query}" 
mà người dùng TikTok Việt Nam đang tìm kiếm.

Yêu cầu:
- Dựa trên xu hướng, viral, trending hiện tại trên TikTok VN
- Bao gồm: tên sản phẩm cụ thể, hashtag phổ biến, câu hỏi người dùng hay tìm, 
  tên người nổi tiếng liên quan, địa điểm hot, event đang diễn ra
- Các gợi ý phải ĐA DẠNG, không trùng lặp, không cùng một pattern
- Viết bằng tiếng Việt, tự nhiên như người thật gõ vào thanh tìm kiếm
- Độ dài gợi ý: 2-6 từ

Chỉ trả về JSON array (không giải thích gì thêm):
["gợi ý 1", "gợi ý 2", ..., "gợi ý {pool_size}"]"""

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],  # Enable Google Search grounding
            "generationConfig": {
                "temperature": 1.0,      # High temperature = more diverse results
                "maxOutputTokens": 2048,
                "topP": 0.95,
            }
        }

        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if 'candidates' not in data or not data['candidates']:
            logger.warning("[Suggest] Gemini returned no candidates")
            return []

        text = data['candidates'][0]['content']['parts'][0]['text'].strip()

        # Parse JSON array from response
        start = text.find('[')
        end = text.rfind(']') + 1
        if start == -1 or end <= start:
            logger.warning(f"[Suggest] Gemini response has no JSON array: {text[:200]}")
            return []

        suggestions = json.loads(text[start:end])
        if isinstance(suggestions, list):
            clean = [s.strip() for s in suggestions if isinstance(s, str) and s.strip() and len(s.strip()) > 1]
            logger.info(f"[Suggest] Gemini pool generated: {len(clean)} suggestions for '{query}'")
            return clean[:pool_size]

        return []

    except requests.exceptions.Timeout:
        logger.warning(f"[Suggest] Gemini TIMEOUT for '{query}'")
        return []
    except Exception as e:
        logger.warning(f"[Suggest] Gemini pool error: {e}")
        return []
