
import os
import django
import json
import logging
import sys

# Setup Django
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import LarkReport, ReportOutstanding
from video_management.utils.lark_utils import get_lark_tenant_access_token, create_bitable_record
from django.conf import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def diagnose():
    try:
        token = get_lark_tenant_access_token()
        logger.info("Successfully obtained Lark Access Token.")
    except Exception as e:
        logger.error(f"Failed to get Lark token: {e}")
        return

    # Check config
    app_id = getattr(settings, "LARK_APP_ID", "")
    base_id = getattr(settings, "LARK_BASE_ID", "")
    table_id = getattr(settings, "LARK_TABLE_ID", "")
    
    logger.info(f"Config: App={app_id}, Base={base_id}, Table={table_id}")

    # Find a report from the screenshot to test
    # e.g., 'local_188f4d85fd7d' (hằng minh) or 'local_232b68c2758e' (yến dung)
    # Let's try to find them in DB
    reports = LarkReport.objects.exclude(name='Unknown').order_by('-date')[:5]
    
    if not reports:
        logger.warning("No reports (excluding Unknown) found in local DB.")
        return

    for report in reports:
        logger.info(f"Testing push for report: {report.id} ({report.name} - {report.date})")
        
        lark_timestamp = int(report.date.timestamp() * 1000)
        lark_report_fields = {
            "HoTen": report.name,
            "Họ tên": report.name,
            "Email": report.email,
            "Date": lark_timestamp,
            "Role": report.role if report.role else "",
            "Team": report.team if report.team else "",
            "Answers": json.dumps(report.answers, ensure_ascii=False) if isinstance(report.answers, dict) else (report.answers or ""),
        }
        
        if report.employee:
            lark_report_fields["Nhân viên"] = report.employee

        try:
            resp = create_bitable_record(token, lark_report_fields)
            if resp.get("code") == 0:
                logger.info(f"SUCCESS: Report {report.id} pushed to Lark. Record ID: {resp.get('data', {}).get('record', {}).get('record_id')}")
            else:
                logger.error(f"FAILURE: Lark returned code {resp.get('code')}: {resp.get('msg')}")
        except Exception as e:
            logger.error(f"ERROR: Failed to push report {report.id}: {e}")

if __name__ == "__main__":
    diagnose()
