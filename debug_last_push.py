
import os
import django
import sys
import json
from django.utils import timezone

# Setup Django
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import LarkReport, ReportOutstanding
from video_management.utils.lark_utils import get_lark_tenant_access_token, create_bitable_record
from django.conf import settings

def check_and_push_latest():
    latest_report = LarkReport.objects.filter(email='baoviet0911@gmail.com').order_by('-created_at').first()
    if not latest_report:
        latest_report = LarkReport.objects.exclude(name='Unknown').order_by('-created_at').first()
    
    if not latest_report:
        print("No reports found.")
        return
    
    print(f"Latest Report: {latest_report.id} ({latest_report.email}, {latest_report.name})")
    
    try:
        token = get_lark_tenant_access_token()
        print("Lark Token obtained.")
    except Exception as e:
        print(f"Token Error: {e}")
        return
    
    sync_date = latest_report.date or latest_report.created_at or timezone.now()
    lark_timestamp = int(sync_date.timestamp() * 1000)
    
    # 1. Main Report
    lark_report_fields = {
        "HoTen": latest_report.name,
        "Email": latest_report.email,
        "Date": lark_timestamp,
        "Answers": json.dumps(latest_report.answers, ensure_ascii=False) if isinstance(latest_report.answers, dict) else (latest_report.answers or ""),
    }
    
    if latest_report.role:
        lark_report_fields["Role"] = latest_report.role
    if latest_report.team:
        lark_report_fields["Team"] = latest_report.team
    if latest_report.employee:
        lark_report_fields["Nhân viên"] = latest_report.employee

    print(f"Pushing Main Report to Table {settings.LARK_TABLE_ID}...")
    try:
        resp = create_bitable_record(token, lark_report_fields)
        if resp.get("code") == 0:
            print(f"SUCCESS: Main Report pushed. ID: {json.dumps(resp.get('data', {}).get('record', {}).get('record_id'))}")
        else:
            print(f"FAILURE: Code {resp.get('code')} - {resp.get('msg')}")
    except Exception as e:
        print(f"ERROR Main: {e}")

if __name__ == "__main__":
    check_and_push_latest()
