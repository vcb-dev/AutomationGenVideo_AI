import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'lark_traffic'
        ORDER BY ordinal_position;
    """)
    rows = cursor.fetchall()
    print("Columns in lark_traffic:")
    for row in rows:
        print(f"  {row[0]}  ({row[1]})")
