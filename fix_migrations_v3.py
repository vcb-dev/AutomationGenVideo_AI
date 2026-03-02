"""
Fix Django migration state v3:
- Migration records already wiped (run v2 above)
- Now check each migration and decide: fake or real
"""
import os
import django
from datetime import datetime

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

existing_tables = set(connection.introspection.table_names())
print(f"📊 Tables in DB ({len(existing_tables)}): {sorted(t for t in existing_tables if 'video' in t)}\n")

def column_exists(table, col):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
        """, [table, col])
        return cursor.fetchone() is not None

recorder = MigrationRecorder(connection)

# Ordered list of all migrations and what they create/need
migrations_plan = [
    # (name, [(op_type, table, col_or_none)])
    ('0001_initial', [
        ('create', 'video_management_scrapedvideo', None),
        ('create', 'video_management_searchhistory', None),
        ('create', 'video_management_trackedchannel', None),
    ]),
    ('0002_collectionvideo_videocollection_and_more', [
        ('create', 'video_management_videocollection', None),
        ('create', 'video_management_collectionvideo', None),
        ('add_col', 'video_management_collectionvideo', 'collection'),
    ]),
    ('0003_trackedchannel_total_videos', [
        ('add_col', 'video_management_trackedchannel', 'total_videos'),
    ]),
    ('0004_remove_trackedchannel_total_videos', []),  # RemoveField - fake if table exists
    ('0005_scrapedvideo_duration_scrapedvideo_feature_vector', [
        ('add_col', 'video_management_scrapedvideo', 'duration'),
        ('add_col', 'video_management_scrapedvideo', 'feature_vector'),
    ]),
    ('0006_facebookpagecache', [
        ('create', 'video_management_facebookpagecache', None),
    ]),
    ('0007_tiktokusercache', [
        ('create', 'video_management_tiktokusercache', None),
    ]),
    ('0008_alter_scrapedvideo_feature_vector_voice', [
        ('add_col', 'video_management_scrapedvideo', 'voice'),
    ]),
    ('0008_reportsettings_alter_scrapedvideo_feature_vector', [
        ('create', 'video_management_reportsettings', None),
    ]),
    ('0009_generatedcontent', [
        ('create', 'video_management_generatedcontent', None),
    ]),
    ('0010_productlist_product', [
        ('create', 'video_management_productlist', None),
        ('create', 'video_management_product', None),
    ]),
    ('0011_add_local_video_cache', [
        ('create', 'video_management_localvideocache', None),
    ]),
    ('0012_add_smart_preprocessing_models', [
        ('create', 'video_management_indexedvideo', None),
        ('create', 'video_management_videoclipcache', None),
    ]),
    ('0013_allow_same_file_different_folder_type', []),
    ('0013_merge_20260208_1341', []),
    ('0014_fix_indexedvideo_unc_paths', []),
    ('0015_searchquery_trendingkeyword', [
        ('create', 'video_management_searchquery', None),
        ('create', 'video_management_trendingkeyword', None),
    ]),
    ('0016_add_search_mode_to_search_history', [
        ('add_col', 'video_management_searchhistory', 'search_mode'),
    ]),
    ('0016_merge_20260210_0936', []),
    ('0017_delete_reportsettings', []),
    ('0018_appuser_larkemployee_larkpermission_larkreport_and_more', [
        ('create', 'video_management_appuser', None),
        ('create', 'video_management_larkemployee', None),
        ('create', 'video_management_larkpermission', None),
        ('create', 'video_management_larkreport', None),
        ('create', 'video_management_reportoutstanding', None),
    ]),
    ('0019_merge_20260302_1045', []),
    ('0020_alter_scrapedvideo_platform_and_more', []),
]

print("🔧 Processing migrations...\n")
real_needed = []

for (mig_name, ops) in migrations_plan:
    needs_real = False
    missing = []
    
    for (op_type, table, col) in ops:
        if op_type == 'create':
            if table not in existing_tables:
                needs_real = True
                missing.append(f"MISSING TABLE: {table}")
        elif op_type == 'add_col':
            if table in existing_tables and not column_exists(table, col):
                needs_real = True
                missing.append(f"MISSING COL: {col} on {table}")
    
    if needs_real:
        real_needed.append(mig_name)
        print(f"🔨 NEED REAL: {mig_name}")
        for m in missing:
            print(f"   └─ {m}")
    else:
        # Record as applied (fake)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                ['video_management', mig_name, datetime.now()]
            )
        print(f"✅ FAKED: {mig_name}")

print(f"\n\n{'='*50}")
print(f"📊 Results:")
print(f"   ✅ Faked: {len(migrations_plan) - len(real_needed)} migrations")
print(f"   🔨 Need real apply: {len(real_needed)} migrations")
if real_needed:
    print(f"\nRun: python manage.py migrate video_management")
    print("To apply the remaining migrations.")
else:
    print("\n✅ All done! No migrations needed.")
