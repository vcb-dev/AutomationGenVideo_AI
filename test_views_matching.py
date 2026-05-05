import requests as _req
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('FACEBOOK_ACCESS_TOKEN')
BASE = "https://graph.facebook.com/v20.0"

me = _req.get(f"{BASE}/me", params={"fields":"id,name","access_token":TOKEN},timeout=15).json()
pid = me["id"]
vids = _req.get(f"{BASE}/{pid}/videos", params={"fields":"id,permalink_url,views,likes.summary(true)","limit":10,"access_token":TOKEN},timeout=20).json().get("data",[])
video_views = {v.get("id",""):v.get("views",0) for v in vids}
print(f"Video views map ({len(video_views)} entries): {video_views}")

posts = _req.get(f"{BASE}/me/posts", params={"fields":"id,permalink_url,likes.summary(true)","limit":10,"access_token":TOKEN},timeout=20).json().get("data",[])

print("\nResult: Posts matched with views via URL ID:")
matched, unmatched = 0, 0
for p in posts:
    permalink = p.get("permalink_url","")
    url_vid_id = ""
    if "/reel/" in permalink:
        url_vid_id = permalink.rstrip("/").rsplit("/reel/", 1)[-1].split("/")[0]
    elif "/videos/" in permalink:
        url_vid_id = permalink.rstrip("/").rsplit("/videos/", 1)[-1].split("/")[0]
    views = video_views.get(url_vid_id, 0) if url_vid_id else 0
    if views > 0:
        status = "MATCHED"
        matched += 1
    elif "/reel/" in permalink or "/videos/" in permalink:
        status = "REEL-NO-VIEW"
        unmatched += 1
    else:
        status = "POST"
        unmatched += 1
    print(f"  {status}: views={views} | url_id={url_vid_id or 'N/A'} | {permalink}")

print(f"\nTotal: {matched} matched with views, {unmatched} no views (normal for text posts)")
