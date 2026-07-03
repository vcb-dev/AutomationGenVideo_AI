"""BrightData Facebook Reels scraper client + DB ingest logic."""

import logging
import requests
from datetime import datetime, timezone as tz_dt
from typing import Optional
from django.conf import settings
from django.utils import timezone

from ..models_scraper import ScrapedFanpage, FacebookReel, FanpageMetricsHistory

logger = logging.getLogger(__name__)

BRIGHTDATA_SCRAPE_URL = "https://api.brightdata.com/datasets/v3/scrape"


def scrape_reels_sync(
    page_url: str,
    num_of_posts: int = 1,
    exclude_post_ids: Optional[list] = None,
    start_date: str = '',
    end_date: str = '',
) -> list:
    """Call BrightData Facebook Reels scraper (sync) and return raw JSON list.

    Args:
        page_url: Clean Facebook page URL (e.g. https://www.facebook.com/katjewelry/)
        num_of_posts: Number of reels to fetch
        exclude_post_ids: Post IDs to skip (dedup)
        start_date/end_date: Date range filter (YYYY-MM-DD)
    """
    api_key = getattr(settings, 'BRIGHTDATA_API_KEY', '')
    dataset_id = getattr(settings, 'BRIGHTDATA_REELS_DATASET_ID', '')
    if not api_key:
        raise ValueError("BRIGHTDATA_API_KEY not configured")
    if not dataset_id:
        raise ValueError("BRIGHTDATA_REELS_DATASET_ID not configured")

    payload_item = {
        "url": page_url,
        "num_of_posts": num_of_posts,
    }
    if exclude_post_ids:
        payload_item["posts_to_not_include"] = exclude_post_ids
    if start_date:
        payload_item["start_date"] = start_date
    if end_date:
        payload_item["end_date"] = end_date

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    url = f"{BRIGHTDATA_SCRAPE_URL}?dataset_id={dataset_id}&format=json"

    logger.info(f"[REELS] Scraping {page_url} (num_of_posts={num_of_posts})")

    import time as _time

    response = requests.post(url, json=[payload_item], headers=headers, timeout=120)
    response.raise_for_status()

    data = response.json()

    # BrightData trả snapshot_id (async) — cần poll cho đến khi ready
    if isinstance(data, dict) and data.get('snapshot_id'):
        snapshot_id = data['snapshot_id']
        logger.info(f"[REELS] Got snapshot_id={snapshot_id}, polling (max 15 min)...")

        snapshot_url = f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json"
        max_polls = 60  # 60 x 15s = 15 phút (đủ cho 300 reels)
        for attempt in range(1, max_polls + 1):
            _time.sleep(15)
            poll_resp = requests.get(snapshot_url, headers=headers, timeout=30)

            if poll_resp.status_code == 202:
                if attempt % 4 == 0:
                    logger.info(f"[REELS] Poll {attempt}/{max_polls}: still building...")
                continue

            if not poll_resp.ok:
                logger.error(f"[REELS] Poll error {poll_resp.status_code}: {poll_resp.text[:300]}")
                return []

            data = poll_resp.json()
            logger.info(f"[REELS] Poll {attempt}: got data, {len(poll_resp.text)} bytes")
            break
        else:
            logger.error(f"[REELS] Timeout polling snapshot {snapshot_id} after 15 min")
            return []

    if isinstance(data, list):
        logger.info(f"[REELS] Got {len(data)} reels from BrightData")
        return data
    return []


def _parse_datetime(date_str: str) -> Optional[datetime]:
    """Parse ISO datetime string from BrightData."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return dt
    except (ValueError, TypeError):
        return None


def enrich_fanpage_from_reel(fanpage: ScrapedFanpage, reel_data: dict) -> None:
    """Update fanpage metadata from reel data (profile_id, followers, avatar, etc)."""
    profile_id = reel_data.get('profile_id', '')
    if profile_id and (not fanpage.profile_id or fanpage.profile_id == ''):
        fanpage.profile_id = str(profile_id)

    fanpage.name = reel_data.get('page_name') or fanpage.name
    fanpage.handle = reel_data.get('profile_handle') or fanpage.handle
    fanpage.avatar_url = reel_data.get('avatar_image_url') or reel_data.get('page_logo') or fanpage.avatar_url
    fanpage.header_image_url = reel_data.get('header_image') or fanpage.header_image_url
    fanpage.is_verified = reel_data.get('page_is_verified')

    page_url = reel_data.get('page_url', '')
    if page_url:
        fanpage.page_url = page_url

    followers = reel_data.get('page_followers', 0) or 0
    likes = reel_data.get('page_likes', 0) or 0
    if followers > fanpage.followers_count:
        fanpage.followers_count = followers
    if likes > fanpage.likes_count:
        fanpage.likes_count = likes

    fanpage.is_visible_on_ui = True
    fanpage.save()

    from ..tasks import upload_thumbnail_to_drive_task
    if fanpage.avatar_url and not fanpage.avatar_drive_url:
        upload_thumbnail_to_drive_task.delay(
            model='facebook_scraped_avatar',
            object_id=fanpage.id,
            cdn_url=fanpage.avatar_url,
            filename=f'fb-scraped-avatar-{fanpage.id}.jpg',
        )
    if fanpage.header_image_url and not fanpage.header_image_drive_url:
        upload_thumbnail_to_drive_task.delay(
            model='facebook_scraped_cover',
            object_id=fanpage.id,
            cdn_url=fanpage.header_image_url,
            filename=f'fb-scraped-cover-{fanpage.id}.jpg',
        )


def ingest_reels_data(reels_list: list, fanpage: Optional[ScrapedFanpage] = None) -> dict:
    """Parse BrightData reels JSON and save to DB.

    1. Enrich/create ScrapedFanpage from first reel's page metadata
    2. Create/update FacebookReel for each reel
    3. Create FanpageMetricsHistory snapshot

    Returns: {'fanpage_id': int, 'created': int, 'updated': int, 'skipped': int}
    """
    if not reels_list:
        return {'fanpage_id': None, 'created': 0, 'updated': 0, 'skipped': 0}

    first_reel = reels_list[0]
    profile_id = str(first_reel.get('profile_id', ''))

    # Upsert fanpage — xử lý dedup theo profile_id
    if profile_id:
        # Kiểm tra profile_id đã tồn tại trong DB chưa
        existing_by_pid = ScrapedFanpage.objects.filter(profile_id=profile_id).first()

        if existing_by_pid:
            # Profile_id đã tồn tại → dùng record đó, xóa placeholder nếu khác
            if fanpage and fanpage.id != existing_by_pid.id:
                fanpage.delete()
            fanpage = existing_by_pid
        elif fanpage:
            # Placeholder chưa có profile_id → gán
            if not fanpage.profile_id or fanpage.profile_id == '':
                fanpage.profile_id = profile_id
                fanpage.save(update_fields=['profile_id'])
        else:
            # Tạo mới
            fanpage, _ = ScrapedFanpage.objects.get_or_create(
                profile_id=profile_id,
                defaults={
                    'name': first_reel.get('page_name', ''),
                    'handle': first_reel.get('profile_handle', ''),
                    'page_url': first_reel.get('page_url', ''),
                }
            )

        enrich_fanpage_from_reel(fanpage, first_reel)
    else:
        return {'fanpage_id': None, 'created': 0, 'updated': 0, 'skipped': 0}

    # Metrics snapshot
    FanpageMetricsHistory.objects.create(
        fanpage=fanpage,
        followers_count=fanpage.followers_count,
        likes_count=fanpage.likes_count,
    )

    # Ingest reels
    created = 0
    updated = 0
    skipped = 0

    for reel in reels_list:
        post_id = reel.get('post_id', '')
        shortcode = reel.get('shortcode', '')
        if not post_id or not shortcode:
            skipped += 1
            continue

        date_posted = _parse_datetime(reel.get('date_posted', ''))
        if not date_posted:
            date_posted = timezone.now()

        hashtags = reel.get('hashtags') or []
        if isinstance(hashtags, list):
            hashtags = [h.lstrip('#') for h in hashtags if h]

        defaults = {
            'fanpage': fanpage,
            'shortcode': shortcode,
            'url': reel.get('url', ''),
            'content': reel.get('content') or '',
            'hashtags': hashtags,
            'video_url': reel.get('video_url', ''),
            'thumbnail_url': reel.get('thumbnail', ''),
            'duration_seconds': reel.get('length'),
            'has_audio': (reel.get('audio', '') == 'AVAILABLE'),
            'date_posted': date_posted,
            'views_count': reel.get('video_view_count', 0) or 0,
            'likes_count': reel.get('likes', 0) or 0,
            'comments_count': reel.get('num_comments', 0) or 0,
            'shares_count': reel.get('num_shares', 0) or 0,
        }

        obj, was_created = FacebookReel.objects.update_or_create(
            post_id=str(post_id),
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1

        thumbnail = reel.get('thumbnail', '')
        if thumbnail and (was_created or not obj.thumbnail_drive_url):
            from ..tasks import upload_thumbnail_to_drive_task
            upload_thumbnail_to_drive_task.delay(
                model='facebook_reel',
                object_id=obj.id,
                cdn_url=thumbnail,
                filename=f'fb-{post_id}.jpg',
            )

    fanpage.last_scraped_at = timezone.now()
    fanpage.scraping_status = 'completed'
    fanpage.scrape_error = None
    if not fanpage.is_initial_scraped and created > 0:
        fanpage.is_initial_scraped = True
    fanpage.save(update_fields=['last_scraped_at', 'scraping_status', 'scrape_error', 'is_initial_scraped', 'updated_at'])

    logger.info(f"[REELS] {fanpage.name}: +{created} new, ~{updated} updated, {skipped} skipped")
    return {'fanpage_id': fanpage.id, 'created': created, 'updated': updated, 'skipped': skipped}


def discover_and_verify_page(page_url: str, fanpage: Optional[ScrapedFanpage] = None) -> Optional[ScrapedFanpage]:
    """Discovery flow: scrape 1 reel to verify page + get metadata.

    Returns ScrapedFanpage if valid page with reels, None if not a real page.
    """
    try:
        reels = scrape_reels_sync(page_url, num_of_posts=1)
    except Exception as e:
        logger.warning(f"[VERIFY] Failed to scrape {page_url}: {e}")
        return None

    if not reels:
        logger.info(f"[VERIFY] No reels found for {page_url} — skipping")
        return None

    result = ingest_reels_data(reels, fanpage=fanpage)
    if result['fanpage_id']:
        return ScrapedFanpage.objects.get(id=result['fanpage_id'])
    return None
