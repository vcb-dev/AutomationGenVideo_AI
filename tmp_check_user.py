import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("SELECT id, email, full_name, is_active FROM users ORDER BY email;")
    rows = cursor.fetchall()
    print(f"Total users: {len(rows)}")
    for r in rows:
        print(f"  {r[1]} | {r[2]} | active={r[3]}")
