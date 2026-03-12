import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("SELECT COUNT(*) FROM lark_traffic;")
    count = cursor.fetchone()[0]
    print(f"Total rows in lark_traffic: {count}")
    
    cursor.execute("SELECT id, name, date, total_traffic, is_confirmed FROM lark_traffic ORDER BY created_at DESC LIMIT 5;")
    rows = cursor.fetchall()
    print("Latest 5 records:")
    for row in rows:
        print(f"  id={row[0][:20]}... name={row[1]} date={row[2]} total={row[3]} confirmed={row[4]}")
