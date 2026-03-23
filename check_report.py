import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'video_core.settings')
django.setup()

from video_management.models import LarkReport

reports = LarkReport.objects.filter(name__icontains='Bảo Việt').order_by('-created_at')[:5]
for r in reports:
    print(f"ID: {r.id}, Name: {r.name}, Email: {r.email}, Date: {r.date}")
