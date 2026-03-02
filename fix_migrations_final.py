"""
Final fix: ensure migrations are recorded in STRICT dependency order
"""
import os
import django
from datetime import datetime

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.db import connection

# First, wipe ALL video_management records
print("🗑️  Wiping all video_management migration records...")
with connection.cursor() as cursor:
    cursor.execute("DELETE FROM django_migrations WHERE app = 'video_management'")
    count = cursor.rowcount
print(f"✅ Deleted {count} records")

existing_tables = set(connection.introspection.table_names())
print(f"\n📊 Tables in DB: {[t for t in sorted(existing_tables) if 'video_management' in t]}\n")

def col_exists(table, col):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
        """, [table, col])
        return bool(cursor.fetchone())

def table_exists(t):
    return t in existing_tables

def fake_record(name):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, %s)",
            ['video_management', name, datetime.now()]
        )
    print(f"   ✅ FAKED: {name}")

# Process in STRICT LINEAR ORDER - only fake what already exists
# Migrations that need REAL apply will be LEFT OUT so Django applies them
migrations_in_order = [
    ('0001_initial', lambda: (
        table_exists('video_management_scrapedvideo') and
        table_exists('video_management_searchhistory') and
        table_exists('video_management_trackedchannel')
    )),
    ('0002_collectionvideo_videocollection_and_more', lambda: (
        table_exists('video_management_videocollection') and
        table_exists('video_management_collectionvideo') and
        col_exists('video_management_collectionvideo', 'collection_id')
    )),
    ('0003_trackedchannel_total_videos', lambda:
        col_exists('video_management_trackedchannel', 'total_videos')
    ),
    ('0004_remove_trackedchannel_total_videos', lambda:
        not col_exists('video_management_trackedchannel', 'total_videos')
    ),
    ('0005_scrapedvideo_duration_scrapedvideo_feature_vector', lambda: (
        col_exists('video_management_scrapedvideo', 'duration') and
        col_exists('video_management_scrapedvideo', 'feature_vector')
    )),
    ('0006_facebookpagecache', lambda:
        table_exists('video_management_facebookpagecache')
    ),
    ('0007_tiktokusercache', lambda:
        table_exists('video_management_tiktokusercache')
    ),
    ('0008_alter_scrapedvideo_feature_vector_voice', lambda:
        col_exists('video_management_scrapedvideo', 'voice')
    ),
    ('0008_reportsettings_alter_scrapedvideo_feature_vector', lambda:
        table_exists('video_management_reportsettings')
    ),
    ('0009_generatedcontent', lambda:
        table_exists('video_management_generatedcontent')
    ),
    ('0010_productlist_product', lambda: (
        table_exists('video_management_productlist') and
        table_exists('video_management_product')
    )),
    ('0011_add_local_video_cache', lambda:
        table_exists('video_management_localvideocache')
    ),
    ('0012_add_smart_preprocessing_models', lambda: (
        table_exists('video_management_indexedvideo') and
        table_exists('video_management_videoclipcache')
    )),
    ('0013_allow_same_file_different_folder_type', lambda:
        table_exists('video_management_indexedvideo')  # already existed = this ran
    ),
    ('0013_merge_20260208_1341', lambda: True),  # merge migration, always fake
    ('0014_fix_indexedvideo_unc_paths', lambda:
        table_exists('video_management_indexedvideo')
    ),
    ('0015_searchquery_trendingkeyword', lambda: (
        table_exists('video_management_searchquery') and
        table_exists('video_management_trendingkeyword')
    )),
    ('0016_add_search_mode_to_search_history', lambda:
        col_exists('video_management_searchhistory', 'search_mode')
    ),
    ('0016_merge_20260210_0936', lambda: True),  # merge, always fake
    ('0017_delete_reportsettings', lambda:
        not table_exists('video_management_reportsettings')  # deleted = migration ran
    ),
    ('0018_appuser_larkemployee_larkpermission_larkreport_and_more', lambda: (
        table_exists('video_management_appuser') and
        table_exists('video_management_larkemployee') and
        table_exists('video_management_larkpermission') and
        table_exists('video_management_larkreport') and
        table_exists('video_management_reportoutstanding')
    )),
    ('0019_merge_20260302_1045', lambda: True),  # merge, always fake after 0018+0016
    ('0020_alter_scrapedvideo_platform_and_more', lambda: True),  # alter, fake
]

print("🔧 Recording migrations in strict order...\n")
faked = 0
skipped = []

for name, can_fake in migrations_in_order:
    if can_fake():
        fake_record(name)
        faked += 1
    else:
        print(f"   🔨 SKIP (needs real): {name}")
        skipped.append(name)

# IMPORTANT: 0019 depends on BOTH 0018 and 0016. 
# If 0018 was skipped (not faked), we need to also skip 0019
if '0018_appuser_larkemployee_larkpermission_larkreport_and_more' in skipped:
    # Remove 0019 from faked if it was recorded
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM django_migrations WHERE app='video_management' AND name='0019_merge_20260302_1045'"
        )
    if '0019_merge_20260302_1045' not in skipped:
        skipped.append('0019_merge_20260302_1045')
        faked -= 1
        print(f"   🔧 Removed 0019 from fake list (depends on 0018 which needs real apply)")
    
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM django_migrations WHERE app='video_management' AND name='0020_alter_scrapedvideo_platform_and_more'"
        )
    if '0020_alter_scrapedvideo_platform_and_more' not in skipped:
        skipped.append('0020_alter_scrapedvideo_platform_and_more')
        faked -= 1

print(f"\n{'='*50}")
print(f"✅ Faked {faked} migrations")
print(f"🔨 {len(skipped)} need real apply: {skipped}")
print(f"\nNow run: python manage.py migrate video_management")
