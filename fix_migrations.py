"""
Fix Django migration state: fake-apply migrations whose tables already exist,
really apply migrations for tables that are missing.
"""
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

# Get all tables currently in DB
existing_tables = set(connection.introspection.table_names())
print(f"\n📊 Tables in DB: {len(existing_tables)}")

# Load migrations
loader = MigrationLoader(connection)
executor = MigrationExecutor(connection)

# Get unapplied migrations
targets = [(key[0], key[1]) for key in loader.disk_migrations.keys() if key[0] == 'video_management']
applied = set(loader.applied_migrations.keys())
unapplied = [t for t in sorted(targets) if t not in applied]

print(f"\n❌ Unapplied migrations: {len(unapplied)}")
for m in unapplied:
    print(f"   - {m[1]}")

if not unapplied:
    print("\n✅ All migrations already applied!")
    sys.exit(0)

# For each unapplied migration, check if we need to fake or really apply
from django.db.migrations.recorder import MigrationRecorder

recorder = MigrationRecorder(connection)

print("\n🔧 Processing migrations...")
for app_label, migration_name in unapplied:
    # Get the migration
    migration = loader.disk_migrations[(app_label, migration_name)]
    
    # Check if this migration only creates tables/columns that already exist
    needs_real_apply = False
    tables_to_create = []
    columns_to_add = []
    
    for operation in migration.operations:
        op_class = operation.__class__.__name__
        
        if op_class == 'CreateModel':
            table_name = f"video_management_{operation.name.lower()}"
            tables_to_create.append(table_name)
            if table_name not in existing_tables:
                needs_real_apply = True
                print(f"   ⚠️  {migration_name}: Missing table '{table_name}'")
        
        elif op_class == 'AddField':
            table_name = f"video_management_{operation.model_name.lower()}"
            col_name = operation.name
            if table_name in existing_tables:
                # Check if column exists
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name = %s AND column_name = %s
                    """, [table_name, col_name])
                    if not cursor.fetchone():
                        columns_to_add.append((table_name, col_name))
                        needs_real_apply = True
                        print(f"   ⚠️  {migration_name}: Missing column '{col_name}' on '{table_name}'")
            else:
                needs_real_apply = True
        
        elif op_class in ('DeleteModel', 'AlterField', 'RenameField', 
                          'RenameModel', 'AlterModelOptions', 'AlterOrderWithRespectTo',
                          'AlterUniqueTogether', 'AlterIndexTogether', 'AddIndex',
                          'RemoveIndex', 'RunSQL', 'RunPython'):
            # These need careful handling - check by trying
            pass
    
    if needs_real_apply:
        print(f"   🔨 REAL apply: {migration_name}")
    else:
        print(f"   ✅ FAKE apply: {migration_name}")
        # Fake apply by recording it
        recorder.record_applied(app_label, migration_name)

print("\n\n📋 Now run: python manage.py migrate video_management")
print("   (Only migrations that need real apply will run)")
