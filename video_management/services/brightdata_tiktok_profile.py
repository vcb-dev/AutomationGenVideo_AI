"""BrightData TikTok Profile Posts scraper + DB ingest logic.

Flow: User nhập profile URL → BrightData cào posts → upsert TikTokProfile + bulk TikTokProfileVideo.
"""

import logging
import time as _time
import requests
from datetime import datetime, timedelta
from typing import Optional
from django.conf import settings
from django.utils import timezone

from ..models_scraper import TikTokProfile, TikTokProfileVideo

logger = logging.getLogger(__name__)

BRIGHTDATA_TRIGGER_URL = "https://api.brightdata.com/datasets/v3/trigger"


def scrape_profile_posts(
    profile_url: str,
    num_of_posts: int = 0,
    exclude_post_ids: Optional[list] = None,
    start_date: str = '',
    end_date: str = '',
    country: str = 'VN',
) -> list:
    """Call BrightData TikTok Posts dataset (discover by profile URL).

    Args:
        profile_url: TikTok profile URL (e.g. https://www.tiktok.com/@himachan.yum)
        num_of_posts: 0 = no limit, 1000 for first scrape
        exclude_post_ids: Post IDs to skip
        start_date: mm-dd-yyyy format
        end_date: mm-dd-yyyy format
        country: Alpha-2 country code
    """
    api_key = getattr(settings, 'BRIGHTDATA_API_KEY', '')
    dataset_id = getattr(settings, 'BRIGHTDATA_TIKTOK_PROFILE_POSTS_DATASET_ID', '')
    if not api_key:
        raise ValueError("BRIGHTDATA_API_KEY not configured")
    if not dataset_id:
        raise ValueError("BRIGHTDATA_TIKTOK_PROFILE_POSTS_DATASET_ID not configured")

    payload_item = {
        "url": profile_url,
        "what_to_collect": "Posts",
        "post_type": "Video Posts",
        "sort_by": "Latest",
    }
    if num_of_posts > 0:
        payload_item["num_of_posts"] = num_of_posts
    if exclude_post_ids:
        payload_item["posts_to_not_include"] = exclude_post_ids
    if start_date:
        payload_item["start_date"] = start_date
    if end_date:
        payload_item["end_date"] = end_date
    if country:
        payload_item["country"] = country

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    url = (
        f"{BRIGHTDATA_TRIGGER_URL}?dataset_id={dataset_id}"
        f"&include_errors=true&type=discover_new&discover_by=profile_url"
    )

    logger.info(f"[TT-PROFILE] Scraping {profile_url} (num={num_of_posts}, country={country})")

    response = requests.post(url, json=[payload_item], headers=headers, timeout=120)

    if not response.ok:
        logger.error(f"[TT-PROFILE] BrightData returned {response.status_code}: {response.text[:500]}")
        response.raise_for_status()

    data = response.json()

    if isinstance(data, dict) and data.get('snapshot_id'):
        snapshot_id = data['snapshot_id']
        logger.info(f"[TT-PROFILE] Got snapshot_id={snapshot_id}, polling (max 20 min)...")

        snapshot_url = f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json"
        max_polls = 80  # 80 x 15s = 20 phút (đủ cho 1000 posts)
        for attempt in range(1, max_polls + 1):
            _time.sleep(15)
            poll_resp = requests.get(snapshot_url, headers=headers, timeout=30)

            if poll_resp.status_code == 202:
                if attempt % 4 == 0:
                    logger.info(f"[TT-PROFILE] Poll {attempt}/{max_polls}: still building...")
                continue

            if not poll_resp.ok:
                logger.error(f"[TT-PROFILE] Poll error {poll_resp.status_code}: {poll_resp.text[:300]}")
                return []

            data = poll_resp.json()
            logger.info(f"[TT-PROFILE] Poll {attempt}: got data, {len(poll_resp.text)} bytes")
            break
        else:
            logger.error(f"[TT-PROFILE] Timeout polling snapshot {snapshot_id}")
            return []

    if isinstance(data, list):
        logger.info(f"[TT-PROFILE] Got {len(data)} posts from BrightData")
        return data
    return []


def _parse_datetime(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def upsert_profile_from_post(post: dict) -> Optional[TikTokProfile]:
    """Upsert TikTokProfile từ data của 1 post (lấy item mới nhất).

    Xử lý case placeholder profile (profile_id='') được tạo sẵn từ view.
    """
    profile_id = str(post.get('profile_id', ''))
    account_id = post.get('account_id', '')
    if not account_id:
        return None

    updates = {
        'nickname': post.get('profile_username', ''),
        'url': post.get('profile_url', ''),
        'avatar_url': post.get('profile_avatar', ''),
        'biography': post.get('profile_biography', ''),
        'is_verified': post.get('is_verified', False) or False,
        'followers_count': post.get('profile_followers', 0) or 0,
    }

    # Tìm profile theo username trước (xử lý placeholder từ view)
    profile = TikTokProfile.objects.filter(username=account_id).first()
    if not profile and profile_id:
        profile = TikTokProfile.objects.filter(profile_id=profile_id).first()

    if profile:
        if profile_id and (not profile.profile_id or profile.profile_id == ''):
            profile.profile_id = profile_id
        for key, value in updates.items():
            setattr(profile, key, value)
        profile.save()
        logger.info(f"[TT-PROFILE] Updated profile: @{account_id}")
    else:
        profile = TikTokProfile.objects.create(
            profile_id=profile_id or '',
            username=account_id,
            **updates,
        )
        logger.info(f"[TT-PROFILE] Created profile: @{account_id}")

    return profile


def ingest_profile_posts(posts: list) -> dict:
    """Parse BrightData TikTok profile posts và lưu vào DB.

    1. Upsert TikTokProfile từ post mới nhất
    2. Upsert TikTokProfileVideo cho mỗi post

    Returns: {'profile_id': int, 'created': int, 'updated': int, 'skipped': int}
    """
    if not posts:
        return {'profile_id': None, 'created': 0, 'updated': 0, 'skipped': 0}

    sorted_posts = sorted(
        posts,
        key=lambda p: p.get('create_time', ''),
        reverse=True,
    )

    profile = upsert_profile_from_post(sorted_posts[0])
    if not profile:
        logger.error("[TT-PROFILE] Cannot extract profile info from posts")
        return {'profile_id': None, 'created': 0, 'updated': 0, 'skipped': 0}

    created = 0
    updated = 0
    skipped = 0

    for v in sorted_posts:
        post_id = v.get('post_id', '')
        if not post_id:
            skipped += 1
            continue

        date_posted = _parse_datetime(v.get('create_time', ''))
        if not date_posted:
            date_posted = timezone.now()

        hashtags = v.get('hashtags') or []
        music = v.get('music') or {}

        defaults = {
            'profile': profile,
            'shortcode': v.get('shortcode') or str(post_id),
            'url': v.get('url') or '',
            'description': v.get('description') or '',
            'hashtags': hashtags,
            'cover_image': v.get('preview_image') or '',
            'video_duration': v.get('video_duration') or 0,
            'region': v.get('region') or '',
            'post_type': v.get('post_type') or 'video',
            'play_count': v.get('play_count') or 0,
            'digg_count': v.get('digg_count') or 0,
            'comment_count': v.get('comment_count') or 0,
            'share_count': int(v.get('share_count') or v.get('num_share_count') or 0),
            'favorites_count': v.get('collect_count') or 0,
            'music_title': music.get('title') or '',
            'music_author': music.get('authorname') or '',
            'original_sound': v.get('original_sound') or '',
            'date_posted': date_posted,
        }

        _, was_created = TikTokProfileVideo.objects.update_or_create(
            video_id=str(post_id),
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1

    logger.info(f"[TT-PROFILE] @{profile.username}: +{created} new, ~{updated} updated, {skipped} skipped")
    return {'profile_id': profile.id, 'created': created, 'updated': updated, 'skipped': skipped}
