"""
Test auto-index manufacturing folders for Dây chuyền
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.smart_preprocessing_service import get_preprocessing_service
from video_management.views.smart_mix_video_views_helper import _auto_index_manufacturing_folders
from video_management.models import IndexedVideo

print("=" * 60)
print("Testing Auto-Index Manufacturing Folders")
print("=" * 60)

# Check current count
current_count = IndexedVideo.objects.filter(
    folder_type="Chế tác",
    file_path__icontains="Dây chuyền"
).count()
print(f"\n📊 Current 'Chế tác' videos with 'Dây chuyền': {current_count}")

# Run auto-index
print(f"\n🔧 Running auto-index for 'Dây chuyền'...")
service = get_preprocessing_service()
_auto_index_manufacturing_folders(service, "Dây chuyền")

# Check new count
new_count = IndexedVideo.objects.filter(
    folder_type="Chế tác",
    file_path__icontains="Dây chuyền"
).count()
print(f"\n📊 After auto-index: {new_count} videos")
print(f"✅ Indexed {new_count - current_count} new videos!")

# Show sample paths
print(f"\n📂 Sample video paths:")
samples = IndexedVideo.objects.filter(
    folder_type="Chế tác",
    file_path__icontains="Dây chuyền"
)[:5]

for video in samples:
    print(f"  - ID {video.id}: {video.file_path[:100]}...")
