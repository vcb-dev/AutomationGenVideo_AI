"""BrightData Google SERP API client for Facebook Fanpage discovery."""

import re
import logging
import requests
from urllib.parse import urlparse, urlencode
from django.conf import settings

logger = logging.getLogger(__name__)

BRIGHTDATA_SCRAPE_URL = "https://api.brightdata.com/datasets/v3/scrape"

# URL patterns that are NOT fanpages
_NON_PAGE_PATTERNS = re.compile(
    r'/groups/|/posts/|/videos/|/photos/|/watch/|/reels/|/reel/|'
    r'/events/|/marketplace/|/stories/|/gaming/|/live/|'
    r'/permalink\.php|/share\.php|/sharer\.php'
)


def call_brightdata_serp_api(keyword_text: str) -> list:
    """Call BrightData Google SERP Dataset API (sync).

    Endpoint: POST /datasets/v3/scrape?dataset_id=...&format=json
    Payload: Array of keyword objects (same format as cURL example).
    """
    api_key = getattr(settings, 'BRIGHTDATA_API_KEY', '')
    dataset_id = getattr(settings, 'BRIGHTDATA_SERP_DATASET_ID', '')
    if not api_key:
        raise ValueError("BRIGHTDATA_API_KEY not configured")
    if not dataset_id:
        raise ValueError("BRIGHTDATA_SERP_DATASET_ID not configured")

    from urllib.parse import quote_plus

    google_query = (
        f'site:facebook.com "{keyword_text}" '
        f'-inurl:posts -inurl:videos -inurl:photos '
        f'-inurl:watch -inurl:reels -inurl:groups -inurl:group'
    )

    search_url = f"https://www.google.com/search?q={quote_plus(google_query)}&gl=VN&hl=vi&num=10"

    payload = [{
        "url": search_url,
    }]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    import time as _time

    url = f"{BRIGHTDATA_SCRAPE_URL}?dataset_id={dataset_id}&format=json&include_errors=true"

    logger.info(f"[SERP] Calling BrightData for keyword: '{keyword_text}'")

    response = requests.post(url, json=payload, headers=headers, timeout=120)

    if not response.ok:
        logger.error(f"[SERP] BrightData returned {response.status_code}: {response.text[:500]}")
        response.raise_for_status()

    data = response.json()

    # BrightData có thể trả snapshot_id (async) — cần poll cho đến khi ready
    if isinstance(data, dict) and data.get('snapshot_id'):
        snapshot_id = data['snapshot_id']
        logger.info(f"[SERP] Got snapshot_id={snapshot_id}, polling for results...")

        snapshot_url = f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json"
        max_polls = 12  # tối đa 12 lần x 10s = 2 phút
        for attempt in range(1, max_polls + 1):
            _time.sleep(10)
            poll_resp = requests.get(snapshot_url, headers=headers, timeout=30)

            if poll_resp.status_code == 202:
                logger.info(f"[SERP] Poll {attempt}/{max_polls}: still building...")
                continue

            if not poll_resp.ok:
                logger.error(f"[SERP] Poll error {poll_resp.status_code}: {poll_resp.text[:300]}")
                return []

            data = poll_resp.json()
            logger.info(f"[SERP] Poll {attempt}: got data, length={len(poll_resp.text)}")
            break
        else:
            logger.error(f"[SERP] Timeout polling snapshot {snapshot_id}")
            return []

    # Parse organic results
    if isinstance(data, list) and len(data) > 0:
        first = data[0]
        organic = first.get('organic', [])
        logger.info(f"[SERP] Got {len(organic)} organic results")
        return organic
    if isinstance(data, dict):
        organic = data.get('organic', [])
        logger.info(f"[SERP] Got {len(organic)} organic results")
        return organic

    logger.warning(f"[SERP] Unexpected response format: {str(data)[:200]}")
    return []


def clean_facebook_url(raw_url: str) -> str:
    """Normalize a Facebook URL to canonical form.

    Examples:
        https://www.facebook.com/katjewelry/?locale=vi_VN  -> https://www.facebook.com/katjewelry/
        https://www.facebook.com/profile.php?id=100064880610399  -> kept as-is (profile ID)
    """
    if not raw_url:
        return ''
    parsed = urlparse(raw_url)
    path = parsed.path.rstrip('/') + '/'

    # profile.php?id=xxx → keep query
    if 'profile.php' in path:
        pid = dict(p.split('=') for p in parsed.query.split('&') if '=' in p).get('id', '')
        if pid:
            return f"https://www.facebook.com/profile.php?id={pid}"

    return f"https://www.facebook.com{path}"


def extract_handle_from_url(url: str) -> str:
    """Extract the page handle from a clean Facebook URL.

    https://www.facebook.com/katjewelry/  -> katjewelry
    https://www.facebook.com/profile.php?id=100064880610399  -> ''
    """
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    if not path or path == 'profile.php':
        return ''
    # Nếu path chứa / (sub-path) thì không phải handle đơn giản
    if '/' in path:
        return ''
    return path


def is_fanpage_url(url: str) -> bool:
    """Check if a Facebook URL is likely a Fanpage (not group/video/post/etc).

    Fanpage URLs look like:
        https://www.facebook.com/katjewelry/
        https://www.facebook.com/profile.php?id=100064880610399
    """
    if not url or 'facebook.com' not in url:
        return False

    # Reject known non-page patterns
    if _NON_PAGE_PATTERNS.search(url):
        return False

    parsed = urlparse(url)
    path = parsed.path.strip('/')

    # profile.php with id → valid page
    if path == 'profile.php' and 'id=' in parsed.query:
        return True

    # Must be a single-segment path (the handle)
    # Reject: path with multiple segments like "username/videos/12345"
    if '/' in path:
        return False

    # Reject empty path (just facebook.com)
    if not path:
        return False

    return True
