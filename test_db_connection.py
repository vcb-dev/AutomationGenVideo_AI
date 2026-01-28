"""
Test Django database connection.
Run this to verify PostgreSQL is being used.
"""
import os
import sys

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

import django
django.setup()

from django.conf import settings
from django.db import connection

print("=" * 80)
print("🔍 DJANGO DATABASE CONFIGURATION")
print("=" * 80)

# Get database settings
db_settings = settings.DATABASES['default']

print(f"\n📊 Database Engine: {db_settings['ENGINE']}")
print(f"📊 Database Name: {db_settings.get('NAME', 'N/A')}")
print(f"📊 Database Host: {db_settings.get('HOST', 'N/A')}")
print(f"📊 Database Port: {db_settings.get('PORT', 'N/A')}")
print(f"📊 Database User: {db_settings.get('USER', 'N/A')}")

# Test connection
print("\n🔌 Testing database connection...")
try:
    with connection.cursor() as cursor:
        cursor.execute("SELECT version();")
        version = cursor.fetchone()[0]
        print(f"✅ Connected successfully!")
        print(f"✅ PostgreSQL Version: {version}")
        
        # Check if it's PostgreSQL
        if 'PostgreSQL' in version:
            print("\n🎉 CONFIRMED: Using PostgreSQL!")
        else:
            print("\n⚠️ WARNING: Not using PostgreSQL!")
            
        # Count videos
        cursor.execute("SELECT COUNT(*) FROM video_management_scrapedvideo;")
        count = cursor.fetchone()[0]
        print(f"\n📹 Total videos in database: {count}")
        
        # Check tables
        cursor.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
            AND table_name LIKE 'video_management%'
            ORDER BY table_name;
        """)
        tables = cursor.fetchall()
        print(f"\n📋 Video Management Tables:")
        for table in tables:
            print(f"  - {table[0]}")
            
except Exception as e:
    print(f"❌ Connection failed: {e}")
    print("\n💡 This might mean:")
    print("  1. PostgreSQL is not running")
    print("  2. Database 'video_production_ai' doesn't exist")
    print("  3. Credentials are incorrect")

print("\n" + "=" * 80)
