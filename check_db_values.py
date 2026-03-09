
import os
import django
import sys

# Setup Django
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import LarkReport

def check_field_values():
    ids = ['local_cb7942823159', 'local_d6925663691a']
    for rid in ids:
        try:
            report = LarkReport.objects.get(id=rid)
            print(f"Report: {rid}")
            print(f"  Name: {report.name}")
            print(f"  Date: {report.date}")
            print(f"  Created At: {report.created_at}")
        except LarkReport.DoesNotExist:
            print(f"Report {rid} not found.")

if __name__ == "__main__":
    check_field_values()
