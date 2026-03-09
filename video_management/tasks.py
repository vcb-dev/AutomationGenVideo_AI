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

from .models import TrackedChannel, SearchHistory, SearchStatus, Platform, LarkReport, ReportOutstanding, LarkEmployee
from .services.apify_service import create_scraper
from .utils.lark_utils import get_lark_tenant_access_token, create_bitable_record
import json
from django.conf import settings

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name='video_management.search_videos',
    max_retries=3,
    default_retry_delay=60
)
def search_videos_task(
    self,
    platform: str,
    keyword: str,
    min_likes: int = 0,
    min_views: int = 0,
    min_comments: int = 0,
    max_results: int = 30,
    page: int = 1,
    use_cache: bool = True,
    search_type: str = 'posts',
    search_mode: str = 'hashtag'
) -> Dict[str, Any]:
    """
    Asynchronous video search task.
    
    Args:
        platform: Platform to search
        keyword: Search keyword
        min_likes: Minimum likes filter
        min_views: Minimum views filter
        max_results: Maximum results
        use_cache: Whether to use cache
        search_type: Type of content (posts, reels)
        
    Returns:
        Search result dictionary
    """
    try:
        logger.info(
            f"[Task {self.request.id}] Starting search: "
            f"platform={platform}, type={search_type}, keyword={keyword}"
        )
        
        scraper = create_scraper(platform, search_type=search_type)
        result = scraper.execute_search(
            keyword=keyword,
            min_likes=min_likes,
            min_views=min_views,
            min_comments=min_comments,
            max_results=max_results,
            page=page,
            search_mode=search_mode,
            use_cache=use_cache,
            save_to_db=True
        )
        
        logger.info(
            f"[Task {self.request.id}] Search completed: "
            f"found {result['count']} videos in {result['execution_time']:.2f}s"
        )
        
        return result
        
    except Exception as e:
        logger.error(
            f"[Task {self.request.id}] Search failed: {str(e)}",
            exc_info=True
        )
        
        # Retry with exponential backoff
        try:
            raise self.retry(exc=e, countdown=2 ** self.request.retries)
        except self.MaxRetriesExceededError:
            return {
                'success': False,
                'error': str(e),
                'count': 0,
                'results': [],
                'execution_time': 0
            }


# DISABLED: Background task removed - channel checks now run synchronously via API
# TikHub integration removed - using Apify only


# DISABLED: Scheduled channel checks removed - use manual checks instead
# @shared_task(name='video_management.check_all_channels')
# def check_all_channels_task() -> Dict[str, Any]:
#     """
#     Check all active tracked channels.
#     
#     This task is scheduled to run periodically via Celery Beat.
#     
#     Returns:
#         Summary of checks performed
#     """
#     logger.info("Starting scheduled channel checks")
#     
#     # Get channels that should be checked
#     now = timezone.now()
#     channels = TrackedChannel.objects.filter(is_active=True)
#     
#     checked = 0
#     skipped = 0
#     
#     for channel in channels:
#         # Check if it's time to check this channel
#         if channel.last_checked_at:
#             next_check = channel.last_checked_at + timedelta(
#                 minutes=channel.check_interval_minutes
#             )
#             if now < next_check:
#                 skipped += 1
#                 continue
#         
#         # Start async check
#         check_channel_task.delay(channel.id)
#         checked += 1
#     
#     logger.info(
#         f"Scheduled checks completed: {checked} started, {skipped} skipped"
#     )
#     
#     return {
#         'success': True,
#         'checked': checked,
#         'skipped': skipped,
#         'total': channels.count()
#     }


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
        outstanding_items = ReportOutstanding.objects.filter(
            email=report.email,
            date=report.date,
            name=report.name
        )
        
        outstanding_table_id = getattr(settings, "LARK_OUTSTANDING_TABLE_ID", "tbluurIuf2qDCdFr")
        
        for item in outstanding_items:
            try:
                # Map fields precisely to tbluurIuf2qDCdFr schema
                lark_out_fields = {
                    "Họ tên": item.name,
                    "Ngày": lark_timestamp,
                    "Team": item.team if item.team else "",
                    "Email": item.email,
                    "Nội dung": item.content,  # User's actual text
                    "Đề xuất": item.category,  # Category label like "KHÓ KHĂN..."
                }

                
                if report.employee:
                    lark_out_fields["Nhân viên"] = report.employee
                
                logger.info(f"Pushing outstanding item format: {item.content}")
                create_bitable_record(lark_token, lark_out_fields, table_id=outstanding_table_id)
            except Exception as e:
                logger.error(f"Error creating outstanding record for report {report_id}: {e}")

                logger.error(f"Error pushing outstanding item {item.id} to Lark: {e}")

        return {"success": True, "lark_id": lark_record_id}

    except LarkReport.DoesNotExist:
        logger.error(f"Report {report_id} not found for background sync")
        return {"success": False, "error": "Report not found"}
    except Exception as e:
        logger.exception(f"Background sync to Lark failed for report {report_id}: {e}")
        return {"success": False, "error": str(e)}
