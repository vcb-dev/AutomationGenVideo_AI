"""
Celery tasks for asynchronous operations.

This module defines background tasks for video scraping, channel monitoring,
and cache cleanup.
"""

import logging
from typing import Dict, Any
from celery import shared_task
from django.utils import timezone
from datetime import timedelta

from .models import TrackedChannel, SearchHistory, SearchStatus, Platform, LarkReport, ReportOutstanding, ManagedFacebookPage
try:
    from .utils.lark_utils import get_lark_tenant_access_token, create_bitable_record  # type: ignore
except Exception:  # pragma: no cover
    get_lark_tenant_access_token = None  # type: ignore
    create_bitable_record = None  # type: ignore
import json
from django.conf import settings

logger = logging.getLogger(__name__)




@shared_task(name='video_management.cleanup_old_cache')
def cleanup_old_cache_task() -> Dict[str, Any]:
    """
    Clean up expired search cache entries.
    
    This task is scheduled to run daily via Celery Beat.
    
    Returns:
        Cleanup summary
    """
    logger.info("Starting cache cleanup")
    
    try:
        # Delete expired cache entries
        expired = SearchHistory.objects.filter(
            expires_at__lt=timezone.now(),
            status=SearchStatus.COMPLETED
        )
        count = expired.count()
        
        if count > 0:
            expired.delete()
            logger.info(f"Deleted {count} expired cache entries")
        else:
            logger.info("No expired cache entries to delete")
        
        # Also clean up very old failed searches (>30 days)
        old_failed = SearchHistory.objects.filter(
            status=SearchStatus.FAILED,
            created_at__lt=timezone.now() - timedelta(days=30)
        )
        failed_count = old_failed.count()
        
        if failed_count > 0:
            old_failed.delete()
            logger.info(f"Deleted {failed_count} old failed searches")
        
        return {
            'success': True,
            'expired_deleted': count,
            'failed_deleted': failed_count
        }
        
    except Exception as e:
        logger.error(f"Cache cleanup failed: {str(e)}", exc_info=True)
        return {
            'success': False,
            'error': str(e)
        }


@shared_task(name='video_management.push_report_to_lark')
def push_report_to_lark_task(report_id: str) -> Dict[str, Any]:
    """
    Background task to push a report and its associated outstanding items to Lark.
    """
    try:
        if get_lark_tenant_access_token is None:
            return {
                "success": False,
                "error": "Missing video_management.utils.lark_utils (get_lark_tenant_access_token).",
            }

        report = LarkReport.objects.get(id=report_id)
        logger.info(f"Starting background sync to Lark for report: {report_id}")
        
        lark_token = get_lark_tenant_access_token()
        
        # 1. Push main report
        sync_date = report.date or report.created_at or timezone.now()
        lark_timestamp = int(sync_date.timestamp() * 1000)
        lark_report_fields = {
            "HoTen": report.name,
            "Email": report.email,
            "Date": lark_timestamp,
            "Answers": json.dumps(report.answers, ensure_ascii=False) if isinstance(report.answers, dict) else (report.answers or ""),
        }
        
        if report.role:
            lark_report_fields["Role"] = report.role
        if report.team:
            lark_report_fields["Team"] = report.team
        if report.employee:
            lark_report_fields["Nhân viên"] = report.employee

        try:
            lark_resp = create_bitable_record(lark_token, lark_report_fields)
        except Exception as e:
            logger.error(f"Error creating bitable record for report {report_id}: {e}")
            return {"status": "error", "message": f"Push to main report failed: {e}"}

        lark_record_id = None
        
        if lark_resp.get("code") == 0:
            lark_record_id = lark_resp.get("data", {}).get("record", {}).get("record_id")
            logger.info(f"Successfully pushed report to Lark: {lark_record_id}")
            # Note: We don't change the local report ID as it's the PK, 
            # but we could store lark_record_id in a separate field if it existed.
        
        # 2. Push Outstanding items
        # Use DD/MM/YYYY since we save it that way in the view
        report_date_str = report.date.strftime("%d/%m/%Y") if report.date else ""
        outstanding_items = ReportOutstanding.objects.filter(
            email=report.email,
            date=report_date_str,
            name=report.name
        )
        
        outstanding_table_id = getattr(settings, "LARK_OUTSTANDING_TABLE_ID", "tbluurIuf2qDCdFr")
        
        for item in outstanding_items:
            try:
                # Map fields precisely to tbluurIuf2qDCdFr schema
                # Based on actual API check: HoTen, Email, Role, Team, Ngày tháng, Phân loại, Nội dung
                lark_out_fields = {
                    "HoTen": item.name,
                    "Email": item.email,
                    "Role": report.role if report.role else "",
                    "Team": item.team if item.team else "",
                    "Ngày tháng": report_date_str, # Field is type Text (1) - using user's format
                    "Phân loại": item.category, # e.g. "KHÓ KHĂN CẦN HỖ TRỢ"
                    "Nội dung": item.content,    # The actual answer text
                }
                
                if report.employee:
                    lark_out_fields["Nhân viên"] = report.employee
                
                logger.info(f"Pushing outstanding item category: {item.category} for {report.name}")
                create_bitable_record(lark_token, lark_out_fields, table_id=outstanding_table_id)
            except Exception as e:
                logger.error(f"Error pushing outstanding item {item.id} to Lark: {e}")


        return {"success": True, "lark_id": lark_record_id}

    except LarkReport.DoesNotExist:
        logger.error(f"Report {report_id} not found for background sync")
        return {"success": False, "error": "Report not found"}
    except Exception as e:
        logger.exception(f"Background sync to Lark failed for report {report_id}: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
#  FACEBOOK SCRAPER — 4 PHASES
#  GĐ0: Import Pages (phát hiện page mới từ Facebook)
#  GĐ1: Backfill (cào lịch sử, chạy 1 lần/page)
#  GĐ2: Delta Sync (cào bài mới, cron hàng ngày)
#  GĐ3: Refresh Metrics (cập nhật views/likes, cron hàng ngày)
# ═══════════════════════════════════════════════════════════

import time

STALE_LOCK_MINUTES = 30
PAGE_COOLDOWN = 10


# ─── GĐ0: IMPORT PAGES (phát hiện page mới) ─────────────

@shared_task(name='video_management.auto_import_pages')
def auto_import_pages_task() -> Dict[str, Any]:
    """GĐ0: Tự động gọi /me/accounts để phát hiện page mới.

    Chạy hàng ngày. Nếu có page mới → tự trigger backfill cho page đó.
    """
    logger.info("═══ [IMPORT] Kiểm tra pages mới từ Facebook ═══")
    try:
        from .services.facebook_fetcher import FacebookFetcher
        fetcher = FacebookFetcher()
        result = fetcher.import_my_managed_pages()
        created = result.get('created', 0)
        updated = result.get('updated', 0)
        logger.info(f"✅ [IMPORT] +{created} page mới, ~{updated} cập nhật")

        # Nếu có page mới → tự trigger backfill
        if created > 0:
            new_pages = ManagedFacebookPage.objects.filter(
                is_active=True, is_backfilled=False
            )
            for page in new_pages:
                logger.info(f"🚀 [IMPORT] Trigger backfill cho page mới: {page.name}")
                backfill_single_page_task.delay(page.page_id)

        return {'success': True, 'created': created, 'updated': updated}
    except Exception as e:
        logger.error(f"❌ [IMPORT] Lỗi: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


def _reset_stale_locks():
    stale_cutoff = timezone.now() - timedelta(minutes=STALE_LOCK_MINUTES)
    stuck = ManagedFacebookPage.objects.filter(is_scraping=True, updated_at__lt=stale_cutoff)
    if stuck.exists():
        count = stuck.count()
        stuck.update(is_scraping=False)
        logger.warning(f"⚠️ Reset {count} page bị stuck lock")


def _lock_page(page):
    page.is_scraping = True
    page.scrape_error = None
    page.save(update_fields=['is_scraping', 'scrape_error', 'updated_at'])


def _unlock_page(page, error: str = None):
    page.is_scraping = False
    page.scrape_error = (error or '')[:500] if error else None
    fields = ['is_scraping', 'scrape_error', 'updated_at']
    if not error:
        page.last_scraped_at = timezone.now()
        fields.append('last_scraped_at')
    page.save(update_fields=fields)


# ─── GĐ1: BACKFILL (cào lịch sử) ────────────────────────

@shared_task(
    bind=True,
    name='video_management.backfill_all_pages',
    time_limit=7200,
    soft_time_limit=7000,
)
def backfill_all_pages_task(self, max_total: int = 300) -> Dict[str, Any]:
    """GĐ1: Cào sâu lịch sử cho các page chưa backfill (is_backfilled=False).

    Chạy thủ công hoặc tự động khi có page mới. Lần theo paging.next.
    """
    _reset_stale_locks()

    pages = ManagedFacebookPage.objects.filter(
        is_active=True, is_backfilled=False, is_scraping=False
    )
    total = pages.count()
    if total == 0:
        logger.info("[BACKFILL] Tất cả pages đã được backfill.")
        return {'success': True, 'total': 0}

    logger.info(f"═══ [BACKFILL] {total} page(s) cần cào lịch sử ═══")
    from .services.facebook_fetcher import FacebookFetcher
    fetcher = FacebookFetcher()

    done = 0
    failed = 0
    errors = []

    for page in pages:
        try:
            _lock_page(page)
            logger.info(f"▶ [{done + failed + 1}/{total}] Backfill: {page.name}")

            result = fetcher.backfill_page(page.page_id, max_total=max_total)
            _unlock_page(page)
            done += 1
            logger.info(f"✅ {page.name}: +{result['created']} mới (quét {result['total_scanned']} bài)")

        except Exception as e:
            _unlock_page(page, error=str(e))
            failed += 1
            errors.append(f"{page.name}: {e}")
            logger.error(f"❌ Backfill {page.name}: {e}", exc_info=True)

        if done + failed < total:
            time.sleep(PAGE_COOLDOWN)

    logger.info(f"═══ [BACKFILL] Xong: {done}/{total} OK, {failed} lỗi ═══")
    return {'success': True, 'total': total, 'done': done, 'failed': failed, 'errors': errors[:10]}


@shared_task(
    bind=True,
    name='video_management.backfill_single_page',
    time_limit=3600,
)
def backfill_single_page_task(self, page_id: str, max_total: int = 300) -> Dict[str, Any]:
    """GĐ1: Backfill 1 page cụ thể (trigger thủ công)."""
    try:
        page = ManagedFacebookPage.objects.get(page_id=page_id)
    except ManagedFacebookPage.DoesNotExist:
        return {'success': False, 'error': f'Page {page_id} không tồn tại'}

    if page.is_scraping:
        return {'success': False, 'error': 'Page đang được xử lý bởi worker khác'}

    try:
        _lock_page(page)
        from .services.facebook_fetcher import FacebookFetcher
        result = FacebookFetcher().backfill_page(page_id, max_total=max_total)
        _unlock_page(page)
        return {'success': True, **result}
    except Exception as e:
        _unlock_page(page, error=str(e))
        logger.error(f"❌ Backfill {page_id}: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


# ─── GĐ2: DELTA SYNC (cào bài mới, cron hàng ngày) ──────

@shared_task(
    bind=True,
    name='video_management.delta_sync_all_pages',
    time_limit=3600,
    soft_time_limit=3300,
)
def delta_sync_all_pages_task(self) -> Dict[str, Any]:
    """GĐ2: Quét bài mới cho các page đã backfill. Chỉ lấy 10 bài gần nhất."""
    _reset_stale_locks()

    pages = ManagedFacebookPage.objects.filter(
        is_active=True, is_backfilled=True, is_scraping=False
    ).order_by('last_scraped_at')

    total = pages.count()
    if total == 0:
        logger.info("[DELTA] Không có page nào cần sync.")
        return {'success': True, 'total': 0}

    logger.info(f"═══ [DELTA] Quét bài mới cho {total} page(s) ═══")
    from .services.facebook_fetcher import FacebookFetcher
    fetcher = FacebookFetcher()

    done = 0
    failed = 0
    errors = []

    for page in pages:
        try:
            _lock_page(page)
            logger.info(f"▶ [{done + failed + 1}/{total}] Delta: {page.name}")

            result = fetcher.sync_page(page_id=page.page_id, max_posts=10)
            _unlock_page(page)
            done += 1
            logger.info(f"✅ {page.name}: +{result.get('created', 0)} mới, ~{result.get('updated', 0)} cập nhật")

        except Exception as e:
            _unlock_page(page, error=str(e))
            failed += 1
            errors.append(f"{page.name}: {e}")
            logger.error(f"❌ Delta {page.name}: {e}", exc_info=True)

        if done + failed < total:
            time.sleep(PAGE_COOLDOWN)

    logger.info(f"═══ [DELTA] Xong: {done}/{total} OK, {failed} lỗi ═══")
    return {'success': True, 'total': total, 'done': done, 'failed': failed, 'errors': errors[:10]}


# ─── GĐ3: REFRESH METRICS (cập nhật views/likes) ─────────

@shared_task(
    bind=True,
    name='video_management.refresh_recent_metrics',
    time_limit=1800,
)
def refresh_recent_metrics_task(self, days: int = 7) -> Dict[str, Any]:
    """GĐ3: Cập nhật metrics cho video đăng trong N ngày gần đây.

    Quét DB, gom ID theo page, gọi batch /?ids= API.
    """
    logger.info(f"═══ [METRICS] Cập nhật metrics cho video {days} ngày gần đây ═══")
    try:
        from .services.facebook_fetcher import FacebookFetcher
        result = FacebookFetcher().refresh_recent_metrics(days=days)
        logger.info(f"═══ [METRICS] Xong: {result['updated']}/{result['total']} video ═══")
        return {'success': True, **result}
    except Exception as e:
        logger.error(f"❌ [METRICS] Lỗi: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


# ─── MANUAL TRIGGER (giữ lại cho API /facebook/sync/) ────

@shared_task(
    bind=True,
    name='video_management.scrape_single_facebook_page',
    max_retries=2,
    default_retry_delay=120,
    time_limit=300,
)
def scrape_single_facebook_page_task(self, page_id: str) -> Dict[str, Any]:
    """Trigger thủ công cào 10 bài mới nhất cho 1 page."""
    try:
        page = ManagedFacebookPage.objects.get(page_id=page_id)
    except ManagedFacebookPage.DoesNotExist:
        return {'success': False, 'error': f'Page {page_id} không tồn tại'}

    if page.is_scraping:
        return {'success': False, 'error': 'Page đang được xử lý bởi worker khác'}

    try:
        _lock_page(page)
        from .services.facebook_fetcher import FacebookFetcher
        result = FacebookFetcher().sync_page(page_id=page_id, max_posts=10)
        _unlock_page(page)
        return {'success': True, 'result': result}
    except Exception as e:
        _unlock_page(page, error=str(e))
        logger.error(f"❌ Manual sync {page_id}: {e}", exc_info=True)
        raise self.retry(exc=e)


# ═══════════════════════════════════════════════════════════
#  GOOGLE SERP — Fanpage Discovery via BrightData
# ═══════════════════════════════════════════════════════════

@shared_task(
    bind=True,
    name='video_management.discover_pages_from_google',
    max_retries=2,
    default_retry_delay=60,
    time_limit=600,
)
def discover_pages_from_google_task(self, keyword_id: int) -> Dict[str, Any]:
    """Discover Facebook Fanpages via Google SERP + verify with Reels.

    Flow:
    1. Google SERP → get candidate URLs
    2. Filter URLs (loại groups/posts)
    3. Cho mỗi candidate: gọi BrightData Reels (num_of_posts=1)
       → Verify page thật + lấy metrics + avatar + 1 sample reel
    4. Lưu ScrapedFanpage (đã có đầy đủ info) + FacebookReel
    """
    from .models_scraper import SearchKeyword, ScrapedFanpage, FanpageKeywordLink
    from .services.brightdata_serp import (
        call_brightdata_serp_api, clean_facebook_url,
        extract_handle_from_url, is_fanpage_url,
    )
    from .services.rapidapi_facebook import discover_and_verify_page

    def _link_keyword(page, kw):
        FanpageKeywordLink.objects.get_or_create(fanpage=page, keyword=kw)

    try:
        keyword = SearchKeyword.objects.get(id=keyword_id)
    except SearchKeyword.DoesNotExist:
        return {'success': False, 'error': f'Keyword ID {keyword_id} not found'}

    logger.info(f"═══ [DISCOVER] '{keyword.cleaned_keyword}' ═══")

    # Step 1: Google SERP
    try:
        organic_results = call_brightdata_serp_api(keyword.cleaned_keyword)
    except Exception as e:
        logger.error(f"❌ [SERP] BrightData API failed: {e}", exc_info=True)
        raise self.retry(exc=e)

    keyword.last_searched_at = timezone.now()
    keyword.save(update_fields=['last_searched_at', 'updated_at'])

    # Step 2: Filter candidate URLs
    candidates = []
    url_skipped = 0
    for item in organic_results:
        raw_link = item.get('link', '')
        title = item.get('title', '')
        if not is_fanpage_url(raw_link):
            url_skipped += 1
            continue
        clean_url = clean_facebook_url(raw_link)
        handle = extract_handle_from_url(clean_url)
        candidates.append({'url': clean_url, 'title': title, 'handle': handle})

    logger.info(f"  URL filter: {len(candidates)} candidates, {url_skipped} skipped")

    # Step 3: Verify each candidate with 1 Reel
    verified = 0
    failed_verify = 0

    for i, cand in enumerate(candidates):
        clean_url = cand['url']
        handle = cand['handle']

        # Dedup: tìm page đã tồn tại theo page_url HOẶC handle (tránh trùng khi URL khác nhưng cùng page)
        existing = ScrapedFanpage.objects.filter(page_url=clean_url).first()
        if not existing and handle:
            existing = ScrapedFanpage.objects.filter(handle=handle).first()

        if existing and existing.profile_id:
            # Page đã verify trước đó → chỉ link keyword, skip verify
            _link_keyword(existing, keyword)
            verified += 1
            logger.info(f"  ✓ Already verified: {existing.name}")
            continue

        # Tạo placeholder nếu chưa có
        if not existing:
            existing = ScrapedFanpage.objects.create(
                profile_id='',
                name=cand['title'][:500] or handle,
                handle=handle,
                page_url=clean_url,
                is_visible_on_ui=False,
            )

        logger.info(f"  [{i+1}/{len(candidates)}] Verifying: {clean_url}")

        page = discover_and_verify_page(clean_url, fanpage=existing)
        if page:
            _link_keyword(page, keyword)
            verified += 1
            logger.info(f"  ✅ Verified: {page.name} ({page.followers_count} followers)")
        else:
            # Không có reels → xóa placeholder
            if not existing.profile_id:
                existing.delete()
            failed_verify += 1
            logger.info(f"  ✗ No reels / not a page: {clean_url}")

        # Cooldown giữa các lần gọi BrightData
        if i < len(candidates) - 1:
            time.sleep(3)

    summary = {
        'success': True,
        'keyword': keyword.cleaned_keyword,
        'total_google_results': len(organic_results),
        'url_filtered': len(candidates),
        'url_skipped': url_skipped,
        'verified': verified,
        'failed_verify': failed_verify,
    }
    logger.info(f"═══ [DISCOVER] Done: {verified} verified, {failed_verify} failed ═══")
    return summary


@shared_task(
    name='video_management.discover_all_active_keywords',
    time_limit=600,
)
def discover_all_active_keywords_task() -> Dict[str, Any]:
    """Cron: Chạy Google discovery cho TẤT CẢ keywords đang active."""
    from .models_scraper import SearchKeyword

    keywords = SearchKeyword.objects.filter(is_google_active=True)
    total = keywords.count()
    if total == 0:
        logger.info("[SERP] No active keywords to discover.")
        return {'success': True, 'total': 0}

    logger.info(f"[SERP] Dispatching {total} keyword(s) for Google discovery")
    for kw in keywords:
        discover_pages_from_google_task.delay(kw.id)
        time.sleep(2)

    return {'success': True, 'dispatched': total}


# ─── SCRAPE REELS FOR A PAGE (manual trigger) ────────────

@shared_task(
    bind=True,
    name='video_management.scrape_reels_for_page',
    max_retries=2,
    default_retry_delay=60,
    time_limit=1200,
)
def scrape_reels_for_page_task(self, fanpage_id: int, num_of_posts: int = 30) -> Dict[str, Any]:
    """Cào batch reels cho 1 fanpage (trigger thủ công từ UI).

    - Lần đầu (is_initial_scraped=False): cào 300 reels
    - Từ lần sau: cào num_of_posts mới, dùng start_date + posts_to_not_include để dedup
    """
    from .models_scraper import ScrapedFanpage, FacebookReel
    from .services.rapidapi_facebook import fetch_page_profile, fetch_reels_only, ingest_reels_data, save_profile_to_db

    try:
        fanpage = ScrapedFanpage.objects.get(id=fanpage_id)
    except ScrapedFanpage.DoesNotExist:
        return {'success': False, 'error': f'Fanpage {fanpage_id} not found'}

    fanpage.scraping_status = 'processing'
    fanpage.save(update_fields=['scraping_status', 'updated_at'])

    is_placeholder = not fanpage.profile_id or fanpage.profile_id.startswith('tmp_')

    try:
        # Bước 1: Cào profile detail
        profile = fetch_page_profile(fanpage.page_url)
        if not profile:
            # Profile fail + đây là bản ghi tạm → xóa luôn
            if is_placeholder:
                fanpage.delete()
                return {'success': False, 'error': 'Không tìm thấy thông tin page từ API'}
            # Profile fail nhưng page đã có dữ liệu cũ → giữ nguyên, báo lỗi
            fanpage.scraping_status = 'failed'
            fanpage.scrape_error = 'Không lấy được profile detail từ API'
            fanpage.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
            return {'success': False, 'error': 'Không lấy được profile detail từ API'}

        # Lưu profile vào DB ngay để FE hiển thị thông tin page trong khi reels vẫn đang cào
        fanpage = save_profile_to_db(profile, fanpage) or fanpage

        # Bước 2: Cào reels
        existing_ids = list(
            FacebookReel.objects.filter(fanpage=fanpage)
            .order_by('-date_posted')
            .values_list('post_id', flat=True)[:500]
        )
        start_date = ''
        if fanpage.is_initial_scraped and fanpage.last_scraped_at:
            start_date = fanpage.last_scraped_at.strftime('%Y-%m-%d')

        effective_num = num_of_posts if fanpage.is_initial_scraped else 300

        reels = fetch_reels_only(
            page_url=fanpage.page_url,
            num_of_posts=effective_num,
            exclude_post_ids=existing_ids if existing_ids else None,
            start_date=start_date,
        )

        # Bước 3: Ingest — profile đã có nên fanpage luôn được lưu dù reels trống
        result = ingest_reels_data(reels, fanpage=fanpage, profile=profile)
        return {'success': True, **result}

    except Exception as e:
        fanpage.scraping_status = 'failed'
        fanpage.scrape_error = str(e)[:500]
        fanpage.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
        logger.error(f"❌ [REELS] Failed {fanpage.name}: {e}", exc_info=True)
        raise self.retry(exc=e)


# ─── PERIODIC SCRAPE (cron 6h sáng cho pages đánh dấu) ───

PERIODIC_COOLDOWN = 5

@shared_task(
    name='video_management.periodic_scrape_marked_pages',
    time_limit=3600,
    soft_time_limit=3400,
)
def periodic_scrape_marked_pages_task() -> Dict[str, Any]:
    """Cron 6h sáng: cào reels mới cho các pages đánh dấu is_periodic_crawl=True.

    Logic:
    - Chỉ cào pages đã initial_scraped (đã cào lượt đầu)
    - start_date = last_scraped_at (chỉ lấy reels mới từ lần cào cuối)
    - posts_to_not_include = 500 post_id gần nhất (double dedup)
    - num_of_posts = 10 (hiếm page nào đăng >10 reels/ngày)
    - Nghỉ giữa mỗi page để tránh rate limit
    """
    from .models_scraper import ScrapedFanpage, FacebookReel
    from .services.rapidapi_facebook import scrape_reels_sync, ingest_reels_data

    pages = ScrapedFanpage.objects.filter(
        is_periodic_crawl=True,
        is_initial_scraped=True,
        is_visible_on_ui=True,
        scraping_status='idle',
    ).order_by('last_scraped_at')

    total = pages.count()
    if total == 0:
        logger.info("[PERIODIC] Không có page nào cần cào định kỳ.")
        return {'success': True, 'total': 0}

    logger.info(f"═══ [PERIODIC] Cào reels mới cho {total} page(s) đánh dấu ═══")

    done = 0
    failed = 0
    total_created = 0
    errors = []

    for i, fanpage in enumerate(pages):
        try:
            fanpage.scraping_status = 'processing'
            fanpage.save(update_fields=['scraping_status', 'updated_at'])

            # Dedup: post_id đã có (500 gần nhất)
            existing_ids = list(
                FacebookReel.objects.filter(fanpage=fanpage)
                .order_by('-date_posted')
                .values_list('post_id', flat=True)[:500]
            )

            # start_date: từ lần cào cuối (hoặc 7 ngày trước nếu chưa cào)
            if fanpage.last_scraped_at:
                start_date = fanpage.last_scraped_at.strftime('%Y-%m-%d')
            else:
                from datetime import datetime, timedelta as td
                start_date = (datetime.now() - td(days=7)).strftime('%Y-%m-%d')

            logger.info(f"  [{i+1}/{total}] {fanpage.name} (start_date={start_date}, exclude={len(existing_ids)} IDs)")

            profile, reels = scrape_reels_sync(
                page_url=fanpage.page_url,
                num_of_posts=10,
                exclude_post_ids=existing_ids if existing_ids else None,
                start_date=start_date,
            )

            result = ingest_reels_data(reels, fanpage=fanpage, profile=profile)
            created = result.get('created', 0)
            total_created += created
            done += 1
            logger.info(f"  ✅ {fanpage.name}: +{created} mới")

        except Exception as e:
            failed += 1
            errors.append(f"{fanpage.name}: {str(e)[:100]}")
            logger.error(f"  ❌ {fanpage.name}: {e}", exc_info=True)
            fanpage.scraping_status = 'failed'
            fanpage.scrape_error = str(e)[:500]
            fanpage.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])

        # Cooldown
        if i < total - 1:
            time.sleep(PERIODIC_COOLDOWN)

    logger.info(f"═══ [PERIODIC] Xong: {done}/{total} OK, +{total_created} reels mới, {failed} lỗi ═══")
    return {
        'success': True,
        'total': total,
        'done': done,
        'failed': failed,
        'total_created': total_created,
        'errors': errors[:10],
    }


# ═══════════════════════════════════════════════════════════
#  TIKTOK — Search by keyword via BrightData
# ═══════════════════════════════════════════════════════════

@shared_task(
    bind=True,
    name='video_management.search_tiktok_keyword',
    max_retries=2,
    default_retry_delay=60,
    time_limit=1200,
)
def search_tiktok_keyword_task(
    self, keyword: str, num_of_posts: int = 30, country: str = 'VN'
) -> Dict[str, Any]:
    """Cào TikTok videos theo keyword qua TikHub API (fallback: BrightData)."""
    logger.info(f"═══ [TIKTOK] Searching: '{keyword}' (num={num_of_posts}) ═══")

    try:
        # Thử TikHub trước
        from .services.tikhub_tiktok import search_tiktok_by_keyword as tikhub_search, ingest_tikhub_videos
        from django.conf import settings

        if getattr(settings, 'TIKHUB_API_KEY', ''):
            logger.info(f"[TIKTOK] Using TikHub API")
            videos = tikhub_search(
                keyword=keyword,
                count=num_of_posts,
                region=country,
            )

            if not videos:
                logger.info(f"[TIKTOK] No videos returned for '{keyword}'")
                return {'success': True, 'keyword': keyword, 'created': 0, 'updated': 0}

            result = ingest_tikhub_videos(videos, search_keyword=keyword)
            logger.info(f"═══ [TIKTOK] Done (TikHub): +{result['created']} videos ═══")
            return {'success': True, 'keyword': keyword, **result}

        # Fallback: BrightData
        logger.info(f"[TIKTOK] TikHub not configured, falling back to BrightData")
        from .services.brightdata_tiktok import search_tiktok_by_keyword, ingest_tiktok_videos
        from .models_scraper import TikTokVideo

        existing_ids = list(
            TikTokVideo.objects.order_by('-date_posted')
            .values_list('post_id', flat=True)[:500]
        )

        videos = search_tiktok_by_keyword(
            keyword=keyword,
            num_of_posts=num_of_posts,
            country=country,
            exclude_post_ids=existing_ids if existing_ids else None,
        )

        if not videos:
            logger.info(f"[TIKTOK] No videos returned for '{keyword}'")
            return {'success': True, 'keyword': keyword, 'created': 0, 'updated': 0}

        result = ingest_tiktok_videos(videos, search_keyword=keyword)
        logger.info(f"═══ [TIKTOK] Done (BrightData): +{result['created']} videos ═══")
        return {'success': True, 'keyword': keyword, **result}

    except Exception as e:
        logger.error(f"❌ [TIKTOK] Failed: {e}", exc_info=True)
        raise self.retry(exc=e)


# ═══════════════════════════════════════════════════════════
#  DOUYIN — Keyword video search via TikHub
# ═══════════════════════════════════════════════════════════

@shared_task(
    bind=True,
    name='video_management.search_douyin_keyword',
    max_retries=2,
    default_retry_delay=60,
    time_limit=1200,
)
def search_douyin_keyword_task(
    self, keyword: str, num_of_posts: int = 30,
) -> Dict[str, Any]:
    """Cào Douyin videos theo keyword qua TikHub fetch_video_search_v2."""
    logger.info(f"═══ [DOUYIN] Searching: '{keyword}' (num={num_of_posts}) ═══")

    try:
        from .services.tikhub_douyin import fetch_douyin_videos, ingest_douyin_videos

        videos = fetch_douyin_videos(keyword=keyword, count=num_of_posts)

        if not videos:
            logger.info(f"[DOUYIN] No videos returned for '{keyword}'")
            return {'success': True, 'keyword': keyword, 'created': 0, 'updated': 0}

        result = ingest_douyin_videos(videos, search_keyword=keyword)
        logger.info(f"═══ [DOUYIN] Done: +{result['created']} new videos ═══")
        return {'success': True, 'keyword': keyword, **result}

    except Exception as exc:
        logger.error(f"[DOUYIN] Task error: {exc}", exc_info=True)
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    name='video_management.scrape_douyin_profile',
    max_retries=2,
    default_retry_delay=60,
    time_limit=1200,
)
def scrape_douyin_profile_task(
    self, profile_id: int, num_of_posts: int = 30,
) -> Dict[str, Any]:
    """Cào Douyin videos từ trang cá nhân của một user qua TikHub.

    Flow:
    1. Load DouyinProfile từ DB, set scraping_status = processing
    2. Gọi TikHub fetch_user_post_videos (pagination)
    3. Upsert profile info từ author của video đầu tiên
    4. Ingest videos vào scraper_douyin_videos (search_keyword = @username)
    5. Update last_scraped_at, is_initial_scraped, scraping_status
    """
    from .models_scraper import DouyinProfile
    from .services.tikhub_douyin import fetch_douyin_user_videos, ingest_douyin_videos

    try:
        profile = DouyinProfile.objects.get(id=profile_id)
    except DouyinProfile.DoesNotExist:
        return {'success': False, 'error': f'Profile {profile_id} not found'}

    logger.info(f"═══ [DOUYIN PROFILE] Scraping @{profile.username} (sec={profile.sec_user_id[:30]}...) num={num_of_posts} ═══")

    profile.scraping_status = 'processing'
    profile.scrape_error = None
    profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])

    try:
        videos = fetch_douyin_user_videos(sec_user_id=profile.sec_user_id, count=num_of_posts)

        if not videos:
            profile.scraping_status = 'idle'
            profile.scrape_error = 'Không có video được trả về (profile không tồn tại hoặc không có video)'
            profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
            return {'success': True, 'profile_id': profile_id, 'created': 0, 'updated': 0}

        # Cập nhật profile info từ author của video đầu tiên
        first_author = (videos[0] if videos else {}).get('author') or {}
        update_fields = ['last_scraped_at', 'is_initial_scraped', 'scraping_status', 'updated_at']

        if first_author:
            for field, src_key in [
                ('uid', 'uid'), ('username', 'unique_id'), ('nickname', 'nickname'),
                ('biography', 'signature'), ('is_verified', 'is_verified'),
            ]:
                val = first_author.get(src_key)
                if val is not None:
                    setattr(profile, field, val)
                    if field not in update_fields:
                        update_fields.append(field)

            # Avatar
            avatar_obj = (first_author.get('avatar_medium') or first_author.get('avatar_larger') or {})
            avatar_urls = avatar_obj.get('url_list') or []
            if avatar_urls:
                profile.avatar_url = avatar_urls[-1]  # lấy URL cuối (thường là jpeg)
                if 'avatar_url' not in update_fields:
                    update_fields.append('avatar_url')
                if not profile.avatar_drive_url:
                    upload_thumbnail_to_drive_task.delay(
                        model='douyin_profile_avatar',
                        object_id=profile.id,
                        cdn_url=profile.avatar_url,
                        filename=f'douyin-avatar-{profile.id}.jpg',
                    )

            followers = first_author.get('follower_count') or first_author.get('followers_count')
            if followers is not None:
                profile.followers_count = int(followers)
                if 'followers_count' not in update_fields:
                    update_fields.append('followers_count')

        # Ingest videos với label @username
        label = f'@{profile.username}' if profile.username else f'@{profile.sec_user_id[:20]}'
        result = ingest_douyin_videos(videos, search_keyword=label)

        profile.is_initial_scraped = True
        profile.last_scraped_at = timezone.now()
        profile.scraping_status = 'idle'
        profile.save(update_fields=update_fields)

        logger.info(f"═══ [DOUYIN PROFILE] Done @{profile.username}: +{result['created']} new videos ═══")
        return {'success': True, 'profile_id': profile_id, 'username': profile.username, **result}

    except Exception as exc:
        logger.error(f"[DOUYIN PROFILE] Task error: {exc}", exc_info=True)
        profile.scraping_status = 'idle'
        profile.scrape_error = str(exc)
        profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
        raise self.retry(exc=exc)


# ═══════════════════════════════════════════════════════════
#  TIKTOK PROFILE — Scrape posts by profile URL via BrightData
# ═══════════════════════════════════════════════════════════

@shared_task(
    bind=True,
    name='video_management.scrape_tiktok_profile_posts',
    max_retries=2,
    default_retry_delay=60,
    time_limit=1800,
)
def scrape_tiktok_profile_posts_task(
    self, profile_id: int, num_of_posts: int = 50, days: int = 0,
) -> Dict[str, Any]:
    """Cào TikTok posts theo profile username qua TikHub.

    - Lần đầu (is_initial_scraped=False): cào num_of_posts bài mới nhất
    - Từ lần sau: cào posts trong `days` ngày gần nhất (thêm mới + cập nhật metrics)
    """
    from .models_scraper import TikTokProfile
    from .services.tikhub_tiktok_profile import fetch_user_posts, ingest_tikhub_profile_posts

    try:
        profile = TikTokProfile.objects.get(id=profile_id)
    except TikTokProfile.DoesNotExist:
        return {'success': False, 'error': f'Profile {profile_id} not found'}

    if profile.scraping_status != 'processing':
        profile.scraping_status = 'processing'
        profile.scrape_error = None
        profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])

    try:
        # Ưu tiên sec_uid (stable) thay vì username (có thể bị user đổi)
        sec_uid = profile.sec_uid or ''
        if profile.is_initial_scraped:
            # Lần sau: cào theo số ngày
            fetch_days = days if days > 0 else 7
            items = fetch_user_posts(profile.username, days=fetch_days, sec_uid=sec_uid)
        else:
            # Lần đầu: cào theo số lượng
            items = fetch_user_posts(profile.username, count=num_of_posts, sec_uid=sec_uid)

        if not items:
            profile.scraping_status = 'idle'
            profile.scrape_error = 'TikHub không trả về posts (profile không có video hoặc username không tồn tại)'
            profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
            return {'success': True, 'profile_id': profile_id, 'created': 0, 'updated': 0}

        if not profile.is_initial_scraped:
            profile.is_initial_scraped = True
            profile.save(update_fields=['is_initial_scraped', 'updated_at'])

        result = ingest_tikhub_profile_posts(items, profile)

        profile.refresh_from_db()
        profile.scraping_status = 'completed'
        profile.scrape_error = None
        profile.last_scraped_at = timezone.now()
        profile.save(update_fields=['scraping_status', 'scrape_error', 'last_scraped_at', 'updated_at'])

        logger.info(f"═══ [TT-PROFILE] Done @{profile.username}: +{result['created']} new ═══")
        return {'success': True, **result}

    except Exception as e:
        profile.scraping_status = 'failed'
        profile.scrape_error = str(e)[:500]
        profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
        logger.error(f"❌ [TT-PROFILE] Failed @{profile.username}: {e}", exc_info=True)

        from django.db import IntegrityError
        if isinstance(e, (IntegrityError, ValueError, TypeError, KeyError)):
            return {'success': False, 'error': str(e)[:500]}
        raise self.retry(exc=e)


TIKTOK_PROFILE_PERIODIC_COOLDOWN = 5

@shared_task(
    name='video_management.periodic_scrape_tiktok_profiles',
    time_limit=3600,
    soft_time_limit=3400,
)
def periodic_scrape_tiktok_profiles_task() -> Dict[str, Any]:
    """Cron: cào posts mới cho các TikTok profiles đánh dấu is_tracked=True.

    Chỉ cào profiles đã initial_scraped. Lấy posts 1 tháng gần nhất.
    """
    from .models_scraper import TikTokProfile

    profiles = TikTokProfile.objects.filter(
        is_tracked=True,
        is_initial_scraped=True,
        scraping_status='idle',
    ).order_by('last_scraped_at')

    total = profiles.count()
    if total == 0:
        logger.info("[TT-PERIODIC] Không có profile nào cần cào định kỳ.")
        return {'success': True, 'total': 0}

    logger.info(f"═══ [TT-PERIODIC] Cào posts mới cho {total} profile(s) ═══")

    done = 0
    failed = 0
    errors = []

    for i, profile in enumerate(profiles):
        try:
            scrape_tiktok_profile_posts_task.delay(profile.id, days=7)
            done += 1
            logger.info(f"  [{i+1}/{total}] Dispatched: @{profile.username}")
        except Exception as e:
            failed += 1
            errors.append(f"@{profile.username}: {str(e)[:100]}")
            logger.error(f"  ❌ @{profile.username}: {e}")

        if i < total - 1:
            time.sleep(TIKTOK_PROFILE_PERIODIC_COOLDOWN)

    logger.info(f"═══ [TT-PERIODIC] Dispatched: {done}/{total}, {failed} lỗi ═══")
    return {'success': True, 'total': total, 'dispatched': done, 'failed': failed, 'errors': errors[:10]}


# ═══════════════════════════════════════════════════════════
#  INSTAGRAM PROFILE — Fetch user info + scrape reels via TikHub
# ═══════════════════════════════════════════════════════════

@shared_task(
    bind=True,
    name='video_management.scrape_instagram_profile_reels',
    max_retries=2,
    default_retry_delay=60,
    time_limit=1800,
)
def scrape_instagram_profile_reels_task(
    self, profile_id: int, num_of_posts: int = 600,
) -> Dict[str, Any]:
    """Fetch user info rồi cào reels qua TikHub.

    Bước 1: fetch_user_info → upsert profile (follower counts, bio, category, v.v.)
    Bước 2: scrape reels (paginated)
    - Lần đầu (is_initial_scraped=False): cào tối đa num_of_posts reels
    - Từ lần sau: cào 50 reels gần nhất
    """
    from .models_scraper import InstagramProfile
    from .services.tikhub_instagram import (
        fetch_user_info, upsert_profile_from_user_info,
        fetch_instagram_reels, ingest_instagram_reels,
    )

    try:
        profile = InstagramProfile.objects.get(id=profile_id)
    except InstagramProfile.DoesNotExist:
        return {'success': False, 'error': f'Profile {profile_id} not found'}

    if profile.scraping_status != 'processing':
        profile.scraping_status = 'processing'
        profile.scrape_error = None
        profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])

    try:
        # Bước 1: fetch user info để có đủ follower counts, bio, v.v.
        user_info = fetch_user_info(profile.username)
        if user_info:
            profile = upsert_profile_from_user_info(profile.username, user_info)
        else:
            logger.warning(f'[IG-PROFILE] fetch_user_info failed for @{profile.username}, continuing with reels only')

        # Bước 2: cào reels
        count = num_of_posts

        reels = fetch_instagram_reels(
            username=profile.username,
            count=count,
        )

        if not reels:
            profile.scraping_status = 'idle'
            profile.scrape_error = 'TikHub không trả về reels (profile không có video hoặc là private)'
            profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
            return {'success': True, 'profile_id': profile_id, 'created': 0, 'updated': 0}

        if not profile.is_initial_scraped:
            profile.is_initial_scraped = True
            profile.save(update_fields=['is_initial_scraped', 'updated_at'])

        result = ingest_instagram_reels(reels)

        profile.refresh_from_db()
        profile.scraping_status = 'completed'
        profile.scrape_error = None
        profile.last_scraped_at = timezone.now()
        profile.save(update_fields=['scraping_status', 'scrape_error', 'last_scraped_at', 'updated_at'])

        logger.info(f"═══ [IG-PROFILE] Done @{profile.username}: +{result['created']} new ═══")
        return {'success': True, **result}

    except Exception as e:
        profile.scraping_status = 'failed'
        profile.scrape_error = str(e)[:500]
        profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
        logger.error(f"❌ [IG-PROFILE] Failed @{profile.username}: {e}", exc_info=True)

        from django.db import IntegrityError
        if isinstance(e, (IntegrityError, ValueError, TypeError, KeyError)):
            return {'success': False, 'error': str(e)[:500]}
        raise self.retry(exc=e)


IG_PROFILE_PERIODIC_COOLDOWN = 5


# ═══════════════════════════════════════════════════════════
#  XIAOHONGSHU PROFILE — Scrape user video notes via TikHub
# ═══════════════════════════════════════════════════════════

@shared_task(
    bind=True,
    name='video_management.scrape_xhs_profile',
    max_retries=2,
    default_retry_delay=60,
    time_limit=1200,
)
def scrape_xhs_profile_task(
    self, profile_id: int, num_of_posts: int = 600,
) -> Dict[str, Any]:
    """Cào video notes từ Xiaohongshu user profile qua TikHub.

    Flow:
    1. Load XiaohongshuProfile, set scraping_status = processing
    2. fetch_xhs_user_video_notes (cursor pagination, filter type=video)
    3. Upsert profile info từ user object của note đầu tiên
    4. ingest_xhs_profile_videos → upsert XiaohongshuVideo
    5. Update last_scraped_at, is_initial_scraped, scraping_status
    """
    from .models_scraper import XiaohongshuProfile
    from .services.tikhub_xiaohongshu import (
        fetch_xhs_user_video_notes, ingest_xhs_profile_videos, upsert_xhs_profile,
    )

    try:
        profile = XiaohongshuProfile.objects.get(id=profile_id)
    except XiaohongshuProfile.DoesNotExist:
        return {'success': False, 'error': f'Profile {profile_id} not found'}

    logger.info(f'═══ [XHS-PROFILE] Scraping user_id={profile.user_id} num={num_of_posts} ═══')

    profile.scraping_status = 'processing'
    profile.scrape_error = None
    profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])

    try:
        notes = fetch_xhs_user_video_notes(user_id=profile.user_id, count=num_of_posts)

        if not notes:
            # Lần đầu cào mà không có data → xóa profile rác
            if not profile.is_initial_scraped:
                logger.info(f'[XHS-PROFILE] Deleting empty profile {profile.user_id} (no notes returned on first scrape)')
                profile.delete()
                return {'success': False, 'deleted': True, 'error': 'User ID không tồn tại hoặc không có video trên Xiaohongshu'}
            # Profile đã cào trước đó → giữ lại, chỉ ghi error
            profile.scraping_status = 'idle'
            profile.scrape_error = 'Không có video mới được trả về'
            profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])
            return {'success': True, 'profile_id': profile_id, 'created': 0, 'updated': 0}

        # Cập nhật profile info từ user object của note đầu tiên
        first_user = (notes[0].get('user') or {}) if notes else {}
        if first_user:
            profile = upsert_xhs_profile(profile.user_id, first_user)

        result = ingest_xhs_profile_videos(notes, profile)

        profile.refresh_from_db()
        profile.is_initial_scraped = True
        profile.last_scraped_at = timezone.now()
        profile.scraping_status = 'completed'
        profile.scrape_error = None
        profile.save(update_fields=[
            'is_initial_scraped', 'last_scraped_at', 'scraping_status', 'scrape_error', 'updated_at',
        ])

        logger.info(f'═══ [XHS-PROFILE] Done user_id={profile.user_id}: +{result["created"]} new ═══')
        return {'success': True, 'profile_id': profile_id, **result}

    except Exception as exc:
        logger.error(f'[XHS-PROFILE] Task error: {exc}', exc_info=True)
        profile.scraping_status = 'idle'
        profile.scrape_error = str(exc)[:500]
        profile.save(update_fields=['scraping_status', 'scrape_error', 'updated_at'])

        from django.db import IntegrityError
        if isinstance(exc, (IntegrityError, ValueError, TypeError, KeyError)):
            return {'success': False, 'error': str(exc)[:500]}
        raise self.retry(exc=exc)


@shared_task(
    name='video_management.periodic_scrape_instagram_profiles',
    time_limit=3600,
    soft_time_limit=3400,
)
def periodic_scrape_instagram_profiles_task() -> Dict[str, Any]:
    """Cron: cào reels mới + cập nhật metrics 7 ngày cho Instagram profiles đánh dấu is_tracked=True."""
    from .models_scraper import InstagramProfile

    profiles = InstagramProfile.objects.filter(
        is_tracked=True,
        is_initial_scraped=True,
        scraping_status='idle',
    ).order_by('last_scraped_at')

    total = profiles.count()
    if total == 0:
        logger.info("[IG-PERIODIC] Không có profile nào cần cào định kỳ.")
        return {'success': True, 'total': 0}

    logger.info(f"═══ [IG-PERIODIC] Cào reels mới cho {total} profile(s) ═══")

    done = 0
    failed = 0
    errors = []

    for i, profile in enumerate(profiles):
        try:
            scrape_instagram_profile_reels_task.delay(profile.id, num_of_posts=10)
            done += 1
            logger.info(f"  [{i+1}/{total}] Dispatched: @{profile.username}")
        except Exception as e:
            failed += 1
            errors.append(f"@{profile.username}: {str(e)[:100]}")
            logger.error(f"  ❌ @{profile.username}: {e}")

        if i < total - 1:
            time.sleep(IG_PROFILE_PERIODIC_COOLDOWN)

    logger.info(f"═══ [IG-PERIODIC] Dispatched: {done}/{total}, {failed} lỗi ═══")
    return {'success': True, 'total': total, 'dispatched': done, 'failed': failed, 'errors': errors[:10]}


# ═══════════════════════════════════════════════════════════
#  DOUYIN PROFILE — Periodic scrape (cron)
# ═══════════════════════════════════════════════════════════

DOUYIN_PROFILE_PERIODIC_COOLDOWN = 5

@shared_task(
    name='video_management.periodic_scrape_douyin_profiles',
    time_limit=3600,
    soft_time_limit=3400,
)
def periodic_scrape_douyin_profiles_task() -> Dict[str, Any]:
    """Cron: cào video mới cho các Douyin profiles đánh dấu is_tracked=True."""
    from .models_scraper import DouyinProfile

    profiles = DouyinProfile.objects.filter(
        is_tracked=True,
        is_initial_scraped=True,
        scraping_status='idle',
    ).order_by('last_scraped_at')

    total = profiles.count()
    if total == 0:
        logger.info("[DOUYIN-PERIODIC] Không có profile nào cần cào định kỳ.")
        return {'success': True, 'total': 0}

    logger.info(f"═══ [DOUYIN-PERIODIC] Cào video mới cho {total} profile(s) ═══")

    done = 0
    failed = 0
    errors = []

    for i, profile in enumerate(profiles):
        try:
            scrape_douyin_profile_task.delay(profile.id, num_of_posts=10)
            done += 1
            logger.info(f"  [{i+1}/{total}] Dispatched: @{profile.username or profile.sec_user_id[:20]}")
        except Exception as e:
            failed += 1
            errors.append(f"@{profile.username}: {str(e)[:100]}")
            logger.error(f"  ❌ @{profile.username}: {e}")

        if i < total - 1:
            time.sleep(DOUYIN_PROFILE_PERIODIC_COOLDOWN)

    logger.info(f"═══ [DOUYIN-PERIODIC] Dispatched: {done}/{total}, {failed} lỗi ═══")
    return {'success': True, 'total': total, 'dispatched': done, 'failed': failed, 'errors': errors[:10]}


# ═══════════════════════════════════════════════════════════
#  XIAOHONGSHU PROFILE — Periodic scrape (cron)
# ═══════════════════════════════════════════════════════════

XHS_PROFILE_PERIODIC_COOLDOWN = 5

@shared_task(
    name='video_management.periodic_scrape_xhs_profiles',
    time_limit=3600,
    soft_time_limit=3400,
)
def periodic_scrape_xhs_profiles_task() -> Dict[str, Any]:
    """Cron: cào video mới cho các Xiaohongshu profiles đánh dấu is_tracked=True."""
    from .models_scraper import XiaohongshuProfile

    profiles = XiaohongshuProfile.objects.filter(
        is_tracked=True,
        is_initial_scraped=True,
        scraping_status='idle',
    ).order_by('last_scraped_at')

    total = profiles.count()
    if total == 0:
        logger.info("[XHS-PERIODIC] Không có profile nào cần cào định kỳ.")
        return {'success': True, 'total': 0}

    logger.info(f"═══ [XHS-PERIODIC] Cào video mới cho {total} profile(s) ═══")

    done = 0
    failed = 0
    errors = []

    for i, profile in enumerate(profiles):
        try:
            scrape_xhs_profile_task.delay(profile.id, num_of_posts=10)
            done += 1
            logger.info(f"  [{i+1}/{total}] Dispatched: @{profile.nickname or profile.user_id}")
        except Exception as e:
            failed += 1
            errors.append(f"@{profile.user_id}: {str(e)[:100]}")
            logger.error(f"  ❌ @{profile.user_id}: {e}")

        if i < total - 1:
            time.sleep(XHS_PROFILE_PERIODIC_COOLDOWN)

    logger.info(f"═══ [XHS-PERIODIC] Dispatched: {done}/{total}, {failed} lỗi ═══")
    return {'success': True, 'total': total, 'dispatched': done, 'failed': failed, 'errors': errors[:10]}


# ═══════════════════════════════════════════════════════════
#  THUMBNAIL UPLOAD TO DRIVE (background, non-blocking)
# ═══════════════════════════════════════════════════════════

@shared_task(
    name='video_management.upload_thumbnail_to_drive',
    max_retries=1,
    default_retry_delay=60,
    time_limit=180,
)
def upload_thumbnail_to_drive_task(
    model: str,
    object_id: int,
    cdn_url: str,
    filename: str,
) -> dict:
    """Upload thumbnail từ CDN URL lên Google Drive và cập nhật DB.

    Args:
        model: 'tiktok_video' hoặc 'tiktok_profile_video'
        object_id: PK của record cần cập nhật
        cdn_url: URL CDN gốc (từ TikTok)
        filename: Tên file lưu trên Drive (vd: tiktok-123456.jpg)
    """
    from .services.drive_upload import upload_thumbnail_from_url, _is_drive_url

    if _is_drive_url(cdn_url):
        return {'skipped': True, 'reason': 'already drive url'}

    drive_url = upload_thumbnail_from_url(cdn_url, filename=filename)
    if not drive_url:
        logger.warning(f"[THUMB-UPLOAD] Upload thất bại {filename} (id={object_id})")
        return {'success': False, 'filename': filename}

    if model == 'tiktok_video':
        from .models_scraper import TikTokVideo
        updated = TikTokVideo.objects.filter(id=object_id).update(preview_image=drive_url)
    elif model == 'tiktok_profile_video':
        from .models_scraper import TikTokProfileVideo
        updated = TikTokProfileVideo.objects.filter(id=object_id).update(cover_image=drive_url)
    elif model == 'douyin_video':
        from .models_scraper import DouyinVideo
        updated = DouyinVideo.objects.filter(id=object_id).update(preview_image=drive_url)
    elif model == 'instagram_reel':
        from .models_scraper import InstagramReel
        updated = InstagramReel.objects.filter(id=object_id).update(thumbnail_drive_url=drive_url)
    elif model == 'xiaohongshu_video':
        from .models_scraper import XiaohongshuVideo
        updated = XiaohongshuVideo.objects.filter(id=object_id).update(thumbnail_drive_url=drive_url)
    elif model == 'facebook_reel':
        from .models_scraper import FacebookReel
        updated = FacebookReel.objects.filter(id=object_id).update(thumbnail_drive_url=drive_url)
    elif model == 'facebook_owned_video':
        from .models import OwnedVideoContent
        updated = OwnedVideoContent.objects.filter(id=object_id).update(thumbnail_drive_url=drive_url)
    elif model == 'tiktok_profile_avatar':
        from .models_scraper import TikTokProfile
        updated = TikTokProfile.objects.filter(id=object_id).update(avatar_drive_url=drive_url)
    elif model == 'instagram_profile_avatar':
        from .models_scraper import InstagramProfile
        updated = InstagramProfile.objects.filter(id=object_id).update(avatar_drive_url=drive_url)
    elif model == 'douyin_profile_avatar':
        from .models_scraper import DouyinProfile
        updated = DouyinProfile.objects.filter(id=object_id).update(avatar_drive_url=drive_url)
    elif model == 'facebook_scraped_avatar':
        from .models_scraper import ScrapedFanpage
        updated = ScrapedFanpage.objects.filter(id=object_id).update(avatar_drive_url=drive_url)
    elif model == 'facebook_scraped_cover':
        from .models_scraper import ScrapedFanpage
        updated = ScrapedFanpage.objects.filter(id=object_id).update(header_image_drive_url=drive_url)
    elif model == 'facebook_managed_avatar':
        from .models import ManagedFacebookPage
        updated = ManagedFacebookPage.objects.filter(id=object_id).update(avatar_drive_url=drive_url)
    else:
        logger.error(f"[THUMB-UPLOAD] Unknown model: {model}")
        return {'success': False, 'reason': 'unknown model'}

    logger.info(f"[THUMB-UPLOAD] {filename} → Drive OK (updated={updated})")
    return {'success': True, 'filename': filename, 'drive_url': drive_url}
