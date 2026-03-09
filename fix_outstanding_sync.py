
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

def fix_unsynced_outstanding():
    # 1. Update existing ISO date records to DD/MM/YYYY
    # The user's screenshot showed ISO format for BẢO VIỆT
    iso_records = ReportOutstanding.objects.filter(date__contains='T')
    print(f"Found {iso_records.count()} records with ISO date format. Updating to DD/MM/YYYY...")
    
    for rec in iso_records:
        try:
            # Simple slice for 2026-03-09T... -> 09/03/2026
            raw = rec.date
            year = raw[0:4]
            month = raw[5:7]
            day = raw[8:10]
            new_date = f"{day}/{month}/{year}"
            rec.date = new_date
            rec.save()
            print(f"Updated record {rec.id} to {new_date}")
        except:
            pass

    # 2. Push latest items for BẢO VIỆT
    token = get_lark_tenant_access_token()
    bast_id = settings.LARK_BASE_ID
    table_id = "tbluurIuf2qDCdFr"
    
    target_name = "BẢO VIỆT"
    target_date = "09/03/2026"
    
    items = ReportOutstanding.objects.filter(name=target_name, date=target_date)
    print(f"Found {items.count()} items to push for {target_name} on {target_date}")
    
    for item in items:
        fields = {
            "HoTen": item.name,
            "Email": item.email,
            "Team": item.team or "",
            "Ngày tháng": item.date,
            "Phân loại": item.category,
            "Nội dung": item.content,
        }
        # Try to find employee info from a report
        report = LarkReport.objects.filter(name=target_name).first()
        if report:
            if report.employee: fields["Nhân viên"] = report.employee
            if report.role: fields["Role"] = report.role

        try:
            resp = create_bitable_record(token, fields, table_id=table_id)
            print(f"Pushed {item.category}: {resp.get('msg')}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    fix_unsynced_outstanding()
