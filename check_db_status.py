
import os
import django
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import IndexedVideo
from django.db.models import Count

print(f"Total Indexed Videos: {IndexedVideo.objects.count()}")
print("-" * 30)
print(f"{'Folder Type':<20} | {'Count':<5}")
print("-" * 30)

stats = IndexedVideo.objects.values('folder_type').annotate(count=Count('id')).order_by('folder_type')
for stat in stats:
    print(f"{stat['folder_type']:<20} | {stat['count']:<5}")
