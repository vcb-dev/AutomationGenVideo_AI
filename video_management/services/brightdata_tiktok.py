"""BrightData TikTok scraper client + DB ingest logic."""

import logging
import time as _time
import requests
from datetime import datetime
from typing import Optional
from django.conf import settings
from django.utils import timezone

from ..models_scraper import TikTokVideo

logger = logging.getLogger(__name__)

BRIGHTDATA_SCRAPE_URL = "https://api.brightdata.com/datasets/v3/scrape"


def search_tiktok_by_keyword(
    keyword: str,
    num_of_posts: int = 30,
    country: str = 'VN',
    exclude_post_ids: Optional[list] = None,
) -> list:
    """Call BrightData TikTok Posts dataset (sync + poll)."""
    api_key = getattr(settings, 'BRIGHTDATA_API_KEY', '')
    dataset_id = getattr(settings, 'BRIGHTDATA_TIKTOK_DATASET_ID', '')
    if not api_key:
        raise ValueError("BRIGHTDATA_API_KEY not configured")
    if not dataset_id:
        raise ValueError("BRIGHTDATA_TIKTOK_DATASET_ID not configured")

    payload_item = {
        "search_keyword": keyword,
        "country": country,
        "num_of_posts": num_of_posts,
    }
    if exclude_post_ids:
        payload_item["posts_to_not_include"] = exclude_post_ids

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    url = (
        f"{BRIGHTDATA_SCRAPE_URL}?dataset_id={dataset_id}"
        f"&format=json&include_errors=true&type=discover_new&discover_by=keyword"
    )

    logger.info(f"[TIKTOK] Searching keyword='{keyword}' num={num_of_posts} country={country}")

    response = requests.post(url, json=[payload_item], headers=headers, timeout=120)

    if not response.ok:
        logger.error(f"[TIKTOK] BrightData returned {response.status_code}: {response.text[:500]}")
        response.raise_for_status()

    data = response.json()

    # Async polling
    if isinstance(data, dict) and data.get('snapshot_id'):
        snapshot_id = data['snapshot_id']
        logger.info(f"[TIKTOK] Got snapshot_id={snapshot_id}, polling (max 10 min)...")

        snapshot_url = f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json"
        max_polls = 40
        for attempt in range(1, max_polls + 1):
            _time.sleep(15)
            poll_resp = requests.get(snapshot_url, headers=headers, timeout=30)

            if poll_resp.status_code == 202:
                if attempt % 4 == 0:
                    logger.info(f"[TIKTOK] Poll {attempt}/{max_polls}: still building...")
                continue

            if not poll_resp.ok:
                logger.error(f"[TIKTOK] Poll error {poll_resp.status_code}: {poll_resp.text[:300]}")
                return []

            data = poll_resp.json()
            logger.info(f"[TIKTOK] Poll {attempt}: got data, {len(poll_resp.text)} bytes")
            break
        else:
            logger.error(f"[TIKTOK] Timeout polling snapshot {snapshot_id}")
            return []

    if isinstance(data, list):
        logger.info(f"[TIKTOK] Got {len(data)} videos from BrightData")
        return data
    return []


def ingest_tiktok_videos(videos: list, search_keyword: str = '') -> dict:
    """Parse BrightData TikTok JSON and save to DB.

    Returns: {'created': int, 'updated': int, 'skipped': int}
    """
    if not videos:
        return {'created': 0, 'updated': 0, 'skipped': 0}

    created = 0
    updated = 0
    skipped = 0

    for v in videos:
        post_id = v.get('post_id', '')
        shortcode = v.get('shortcode', '') or post_id
        if not post_id:
            skipped += 1
            continue

        date_posted = None
        raw_date = v.get('create_time', '')
        if raw_date:
            try:
                date_posted = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                pass
        if not date_posted:
            date_posted = timezone.now()

        hashtags = v.get('hashtags') or []
        music = v.get('music') or {}

        defaults = {
            'shortcode': shortcode,
            'url': v.get('url', ''),
            'description': v.get('description') or '',
            'hashtags': hashtags,
            'video_url': v.get('video_url', ''),
            'cdn_url': v.get('cdn_url') or v.get('cdn_link') or '',
            'preview_image': v.get('preview_image', ''),
            'video_duration': v.get('video_duration', 0) or 0,
            'region': v.get('region', ''),
            # Author
            'author_id': str(v.get('profile_id', '')),
            'author_username': v.get('account_id') or '',
            'author_display_name': v.get('profile_username') or '',
            'author_avatar': v.get('profile_avatar', ''),
            'author_url': v.get('profile_url', ''),
            'author_followers': v.get('profile_followers', 0) or 0,
            'author_is_verified': v.get('is_verified', False) or False,
            # Metrics
            'play_count': v.get('play_count', 0) or 0,
            'digg_count': v.get('digg_count', 0) or 0,
            'comment_count': v.get('comment_count', 0) or 0,
            'share_count': int(v.get('share_count', 0) or v.get('num_share_count', 0) or 0),
            'collect_count': v.get('collect_count', 0) or 0,
            # Music
            'music_title': music.get('title', ''),
            'music_author': music.get('authorname', ''),
            'original_sound': v.get('original_sound', ''),
            # Discovery
            'search_keyword': search_keyword,
            'date_posted': date_posted,
        }

        _, was_created = TikTokVideo.objects.update_or_create(
            post_id=str(post_id),
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1

    logger.info(f"[TIKTOK] Ingest: +{created} new, ~{updated} updated, {skipped} skipped")
    return {'created': created, 'updated': updated, 'skipped': skipped}
