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



# Facebook external fanpages (scrape_reels_for_page_task / periodic_scrape_marked_pages_task):
# đã chuyển sang BE (FacebookExternalScraperCronService trong AutomationGenVideo_BE).

# Xiaohongshu profile periodic scrape: đã chuyển sang BE
# (XiaohongshuScraperCronService trong AutomationGenVideo_BE).
