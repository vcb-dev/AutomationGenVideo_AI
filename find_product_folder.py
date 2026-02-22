
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.smart_preprocessing_service import get_preprocessing_service

service = get_preprocessing_service()

CATEGORY = "Dây chuyền"
SEARCH_TERM = "cỏ 4 lá" # From product name

print(f"🕵️ Searching for '{SEARCH_TERM}' in '{CATEGORY}' category...")

# 1. auto_index helper has logic to find category folder
from video_management.views.smart_mix_video_views_helper import _auto_index_manufacturing_folders

# Let's use service directly to find category folder first
base_paths = [
    r"\\VCB_MEDIA\MEDIA VCB folder\CHẾ TÁC SẢN PHẨM (xưởng)\Việt Nam",
    r"\\192.168.1.250\MEDIA VCB folder\CHẾ TÁC SẢN PHẨM (xưởng)\Việt Nam"
]

cat_path = None
for base in base_paths:
    if os.path.exists(base):
        print(f"📂 Checking base: {base}")
        found = service.find_folder_by_name(base, CATEGORY, max_depth=1)
        if found:
            cat_path = found
            break

if not cat_path:
    print(f"❌ Category folder '{CATEGORY}' not found!")
    exit()

print(f"✅ Found Category Folder: {cat_path}")

# 2. Search for subfolder matching product name
print(f"🔍 Scanning subfolders for '{SEARCH_TERM}'...")
found_product = service.find_folder_by_name(cat_path, SEARCH_TERM, exact_match=False, max_depth=2)

if found_product:
    print(f"✅ FOUND PRODUCT FOLDER: {found_product}")
    # Extract SKU from folder name? Typically "SKU_Name"
    folder_name = os.path.basename(found_product)
    print(f"📦 Suggested SKU/Folder Name: {folder_name}")
else:
    print(f"❌ No folder found containing '{SEARCH_TERM}'")
