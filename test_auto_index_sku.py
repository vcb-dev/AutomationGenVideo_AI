"""
Test auto-index folders with SKU logic
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.smart_preprocessing_service import get_preprocessing_service
from video_management.views.smart_mix_video_views_helper import _auto_index_manufacturing_folders
from video_management.models import IndexedVideo

service = get_preprocessing_service()

def check_indexed_count():
    count = IndexedVideo.objects.filter(folder_type="Chế tác").count()
    return count

print("="*50)
print("Testing Auto-Index with SKU Logic")
print("="*50)

# 1. Clear current index for clean test
print("🧹 Cleaning 'Chế tác' index...")
IndexedVideo.objects.filter(folder_type="Chế tác").delete()

# 2. Run Test
# Use real SKU from your system logs: "M814002" inside "Dây chuyền"
SKU = "M814002" 
CATEGORY = "Dây chuyền"

print(f"🔧 Running auto-index for Category: '{CATEGORY}', SKU: '{SKU}'...")
_auto_index_manufacturing_folders(service, CATEGORY, product_sku=SKU)

# 3. Verify results
count = check_indexed_count()
print(f"📊 Indexed videos in 'Chế tác': {count}")

if count > 0 and count < 100:
    print("✅ SUCCESS! Indexed a specific folder (count is small/reasonable).")
elif count > 500:
    print("⚠️ WARNING: Count is very high. Check if it fallback to category level indexing?")
else:
    print("❌ FAILED: No videos indexed.")
