
import os
import django
import sys

# Setup Django
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import ReportOutstanding

def check_outstanding():
    items = ReportOutstanding.objects.all().order_by('-created_at')[:10]
    print(f"Total count: {ReportOutstanding.objects.count()}")
    for item in items:
        print(f"ID: {item.id}, Name: {item.name}, Category: {item.category}, Content: {item.content}")

if __name__ == "__main__":
    check_outstanding()
