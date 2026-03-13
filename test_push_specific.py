
import os
import django
import json
import logging
import sys
from datetime import datetime

# Setup Django
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import LarkReport, ReportOutstanding
from video_management.utils.lark_utils import get_lark_tenant_access_token, create_bitable_record
from django.conf import settings
from django.utils import timezone

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_specific_ids():
    target_ids = ['local_cb7942823159', 'local_d6925663691a']
    
    try:
        token = get_lark_tenant_access_token()
        logger.info("Lark Token obtained.")
    except Exception as e:
        logger.error(f"Lark Token Error: {e}")
        return

    for rid in target_ids:
        try:
            report = LarkReport.objects.get(id=rid)
            logger.info(f"Processing Report: {rid} ({report.name})")
            
            # Use my fix logic
            sync_date = report.date or report.created_at or timezone.now()
            lark_timestamp = int(sync_date.timestamp() * 1000)
            
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

            logger.info(f"Pushing fields: {json.dumps(lark_report_fields, ensure_ascii=False)}")
            
            resp = create_bitable_record(token, lark_report_fields)
            logger.info(f"Response: {json.dumps(resp, ensure_ascii=False)}")
            
        except LarkReport.DoesNotExist:
            logger.error(f"Report {rid} not found in DB.")
        except Exception as e:
            logger.error(f"Error pushing {rid}: {e}")

if __name__ == "__main__":
    test_specific_ids()
