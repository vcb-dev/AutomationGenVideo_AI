"""
Fix Django migration state completely:
1. Delete ALL video_management migration records from DB
2. Get list of all existing tables
3. For each migration in dependency order:
   - If it only creates tables/columns that ALREADY EXIST → fake record it
   - If it creates something MISSING → queue for real apply
4. Apply real migrations in sequence
"""
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.loader import MigrationLoader

# Step 1: Delete ALL video_management migration records
print("🗑️  Clearing all video_management migration records from DB...")
with connection.cursor() as cursor:
    cursor.execute(
        "DELETE FROM django_migrations WHERE app = 'video_management'"
    )
print(f"✅ Done")

# Step 2: Get existing tables and columns
existing_tables = set(connection.introspection.table_names())
print(f"\n📊 Tables in DB: {len(existing_tables)}")

def column_exists(table_name, column_name):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
        """, [table_name, column_name])
        return cursor.fetchone() is not None

# Step 3: Load migrations in dependency order
loader = MigrationLoader(connection, ignore_no_migrations=True)
recorder = MigrationRecorder(connection)

# Get migration graph in topological order
graph = loader.graph
app = 'video_management'

# Get all leaf nodes for video_management and resolve full chain
leaf_nodes = [node for node in graph.leaf_nodes() if node[0] == app]
all_migrations = []
seen = set()

def resolve_deps(node):
    if node in seen:
        return
    if node[0] != app:
        return
    seen.add(node)
    migration = graph.node_map[node].migration
    for dep in migration.dependencies:
        if dep[0] == app:
            resolve_deps(dep)
    all_migrations.append(node)

for leaf in leaf_nodes:
    resolve_deps(leaf)

print(f"\n📋 Processing {len(all_migrations)} migrations in order...")

fake_these = []
real_these = []

for node in all_migrations:
    migration = loader.disk_migrations[node]
    migration_name = node[1]
    needs_real = False
    reasons = []
    
    for op in migration.operations:
        op_name = op.__class__.__name__
        
        if op_name == 'CreateModel':
            table = f"video_management_{op.name.lower()}"
            if table not in existing_tables:
                needs_real = True
                reasons.append(f"MISSING TABLE: {table}")
        
        elif op_name == 'AddField':
            table = f"video_management_{op.model_name.lower()}"
            col = op.name
            if table in existing_tables and not column_exists(table, col):
                needs_real = True
                reasons.append(f"MISSING COLUMN: {col} on {table}")
            elif table not in existing_tables:
                needs_real = True
                reasons.append(f"TABLE NOT FOUND: {table}")
        
        # DeleteModel, AlterField etc. → fake them (table already handled)
    
    if needs_real:
        real_these.append((node, reasons))
        print(f"   🔨 REAL: {migration_name}")
        for r in reasons:
            print(f"      └─ {r}")
    else:
        fake_these.append(node)
        print(f"   ✅ FAKE: {migration_name}")

# Step 4: Record all FAKE migrations
print(f"\n📝 Recording {len(fake_these)} fake migrations...")
for node in fake_these:
    recorder.record_applied(node[0], node[1])
    print(f"   ✅ Recorded: {node[1]}")

print(f"\n🔨 {len(real_these)} migrations need REAL apply:")
for node, reasons in real_these:
    print(f"   - {node[1]}")
    for r in reasons:
        print(f"     └─ {r}")

print("\n\n✅ All fake migrations recorded!")
print("▶️  Now run: python manage.py migrate video_management")
print("   This will only apply migrations for MISSING tables/columns")
