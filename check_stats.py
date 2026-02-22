import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import IndexedVideo
from django.db.models import Count

print("\n" + "="*70)
print("DATABASE STATS AFTER MIGRATION")
print("="*70 + "\n")

stats = IndexedVideo.objects.filter(is_available=True).values('folder_type').annotate(count=Count('id')).order_by('-count')

for s in stats:
    print(f"  • {s['folder_type']}: {s['count']} videos")

total = IndexedVideo.objects.filter(is_available=True).count()
print(f"\n  Total: {total} videos")

print("\n" + "="*70)
