import os
import requests
import django
import json

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
token = getattr(settings, 'FACEBOOK_ACCESS_TOKEN', '')
url = "https://graph.facebook.com/v20.0/763339010200340_122126069817006075"
params = {
    "fields": "message,comments.summary(true),attachments{media,target,subattachments}",
    "access_token": token
}
r = requests.get(url, params=params)
data = r.json()
print("Post level comments:", data.get('comments', {}).get('summary', {}).get('total_count'))

if 'attachments' in data:
    for att in data['attachments']['data']:
        if 'target' in att and 'id' in att['target']:
            target_id = att['target']['id']
            url2 = f"https://graph.facebook.com/v20.0/{target_id}"
            r2 = requests.get(url2, params={"fields": "comments.summary(true)", "access_token": token})
            target_data = r2.json()
            count = target_data.get('comments', {}).get('summary', {}).get('total_count', 0)
            print(f"Attachment {target_id} comments:", count)
        
        # Check subattachments if it's an album
        if 'subattachments' in att:
            for sub in att['subattachments']['data']:
                if 'target' in sub and 'id' in sub['target']:
                    target_id = sub['target']['id']
                    url2 = f"https://graph.facebook.com/v20.0/{target_id}"
                    r2 = requests.get(url2, params={"fields": "comments.summary(true)", "access_token": token})
                    target_data = r2.json()
                    count = target_data.get('comments', {}).get('summary', {}).get('total_count', 0)
                    print(f"Sub-Attachment {target_id} comments:", count)
