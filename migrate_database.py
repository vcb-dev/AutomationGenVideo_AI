#!/usr/bin/env python
"""
Database Migration Helper Script
Migrates data from video_production_ai to video_production database
"""

import os
import sys
import django
from datetime import datetime

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.db import connection
from video_management.models import SearchHistory, ScrapedVideo, TrackedChannel, VideoCollection, CollectionVideo


def check_database_connection():
    """Verify database connection"""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), version();")
            db_name, version = cursor.fetchone()
            print(f"✓ Connected to database: {db_name}")
            print(f"✓ PostgreSQL version: {version.split(',')[0]}")
            return True
    except Exception as e:
        print(f"✗ Database connection failed: {e}")
        return False


def check_old_database_exists():
    """Check if old AI database exists"""
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT datname FROM pg_database 
                WHERE datname = 'video_production_ai';
            """)
            result = cursor.fetchone()
            if result:
                print("✓ Old database 'video_production_ai' found")
                return True
            else:
                print("✗ Old database 'video_production_ai' not found")
                return False
    except Exception as e:
        print(f"✗ Error checking old database: {e}")
        return False


def get_table_counts():
    """Get record counts from current database"""
    counts = {
        'SearchHistory': SearchHistory.objects.count(),
        'ScrapedVideo': ScrapedVideo.objects.count(),
        'TrackedChannel': TrackedChannel.objects.count(),
        'VideoCollection': VideoCollection.objects.count(),
        'CollectionVideo': CollectionVideo.objects.count(),
    }
    return counts


def display_counts(title, counts):
    """Display table counts"""
    print(f"\n{title}")
    print("=" * 50)
    for table, count in counts.items():
        print(f"  {table:20s}: {count:6d} records")
    print("=" * 50)


def verify_migration():
    """Verify migration was successful"""
    print("\n" + "=" * 60)
    print("MIGRATION VERIFICATION")
    print("=" * 60)
    
    counts = get_table_counts()
    display_counts("Current Database Record Counts", counts)
    
    total = sum(counts.values())
    if total > 0:
        print(f"\n✓ Migration appears successful! Total records: {total}")
        return True
    else:
        print("\n⚠ Warning: No records found. Migration may not have completed.")
        return False


def run_sql_migration():
    """Execute the SQL migration script"""
    sql_file = os.path.join(os.path.dirname(__file__), 'migrate_to_be_database.sql')
    
    if not os.path.exists(sql_file):
        print(f"✗ SQL migration file not found: {sql_file}")
        return False
    
    print(f"\n✓ Found SQL migration file: {sql_file}")
    print("\nTo run the SQL migration, execute:")
    print(f"  psql -U postgres -d video_production -f {sql_file}")
    print("\nOr if you have a different user:")
    print(f"  psql -U your_username -d video_production -f {sql_file}")
    
    return True


def backup_reminder():
    """Remind user to backup databases"""
    print("\n" + "!" * 60)
    print("IMPORTANT: BACKUP REMINDER")
    print("!" * 60)
    print("\nBefore proceeding, make sure you have backed up both databases:")
    print("\n  # Backup AI database")
    print("  pg_dump -U postgres -d video_production_ai > backup_ai.sql")
    print("\n  # Backup BE database")
    print("  pg_dump -U postgres -d video_production > backup_be.sql")
    print("\n" + "!" * 60)


def main():
    """Main migration workflow"""
    print("\n" + "=" * 60)
    print("DATABASE MIGRATION HELPER")
    print("From: video_production_ai → To: video_production")
    print("=" * 60)
    
    # Step 1: Backup reminder
    backup_reminder()
    
    # Step 2: Check database connection
    print("\n[Step 1] Checking database connection...")
    if not check_database_connection():
        print("\n✗ Cannot proceed without database connection")
        return
    
    # Step 3: Display current state
    print("\n[Step 2] Checking current database state...")
    counts_before = get_table_counts()
    display_counts("Records in Current Database (BEFORE migration)", counts_before)
    
    # Step 4: Check if old database exists
    print("\n[Step 3] Checking for old database...")
    old_db_exists = check_old_database_exists()
    
    # Step 5: Provide SQL migration instructions
    print("\n[Step 4] SQL Migration Instructions...")
    run_sql_migration()
    
    # Step 6: Ask user to run migration
    print("\n" + "=" * 60)
    print("NEXT STEPS:")
    print("=" * 60)
    print("\n1. Run the SQL migration script (command shown above)")
    print("2. Run this script again with --verify flag to check results:")
    print(f"   python {os.path.basename(__file__)} --verify")
    print("\n" + "=" * 60)


def verify_only():
    """Run verification only"""
    print("\n" + "=" * 60)
    print("VERIFICATION MODE")
    print("=" * 60)
    
    if not check_database_connection():
        return
    
    verify_migration()
    
    # Show sample data
    print("\n[Sample Data Check]")
    print("-" * 60)
    
    if SearchHistory.objects.exists():
        latest = SearchHistory.objects.first()
        print(f"Latest SearchHistory: {latest.platform} - {latest.keyword}")
    
    if ScrapedVideo.objects.exists():
        latest = ScrapedVideo.objects.first()
        print(f"Latest ScrapedVideo: {latest.platform} - @{latest.author_username}")
    
    if TrackedChannel.objects.exists():
        latest = TrackedChannel.objects.first()
        print(f"Latest TrackedChannel: {latest.platform} - @{latest.username}")
    
    print("-" * 60)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--verify':
        verify_only()
    else:
        main()
