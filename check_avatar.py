import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("SELECT * FROM huyk_channels WHERE channel_id = '61580182263005'")
    cols = [desc[0] for desc in cursor.description]
    row = cursor.fetchone()
    print("huyk_channels:")
    if row:
        for idx, val in enumerate(row):
            print(f"  {cols[idx]}: {val}")
    else:
        print("  None")
    
    cursor.execute("SELECT * FROM video_management_facebookpagecache WHERE username = '61580182263005'")
    cols = [desc[0] for desc in cursor.description]
    row = cursor.fetchone()
    print("\nfacebook_page_cache:")
    if row:
        for idx, val in enumerate(row):
            print(f"  {cols[idx]}: {val}")
    else:
        print("  None")
