"""
Quick test: verify Graph API hook correctly returns views via /videos endpoint.
Run: python test_graph_views_hook.py
"""

import requests as _req
import json

TOKEN = "EAASfB9OySdwBRN9lm5WpzpLMKoTG69JYdvZANXeWV5kAXVZBtJmBwycwZCRxNC0gglbW1XZBumAmFiUhJiQllv6eqiW8MDjOkyq60gvKJzYUrVqEmx15GQ936HxkAq7gdymIUMcUHNwislK0jmWbjcV1Qdou1oBxLzUZBlFlCiOL6KzDfzqVhITMAZBwdVRwZC4ZAhTPDp4YS3LTZAoQSm3PCDJrNZCR9dX3ReVDVBZBaieZCTMZD"
BASE = "https://graph.facebook.com/v20.0"

def main():
    # Step 1: get page info
    me = _req.get(f"{BASE}/me", params={"fields":"id,name","access_token":TOKEN},timeout=15).json()
    page_id = me["id"]
    page_name = me["name"]
    print(f"Page: {page_name} (id={page_id})")

    # Step 2: get videos with views
    vid_r = _req.get(f"{BASE}/{page_id}/videos", params={
        "fields": "id,title,description,created_time,permalink_url,picture,likes.summary(true),comments.summary(true),views",
        "limit": 10,
        "access_token": TOKEN
    }, timeout=20)
    vids = vid_r.json().get("data", [])
    
    video_views_map = {}
    print(f"\n=== Videos from /{page_id}/videos ===")
    for v in vids:
        vid_id = v.get("id","")
        views = v.get("views", 0) or 0
        likes = v.get("likes", {}).get("summary", {}).get("total_count", 0)
        video_views_map[vid_id] = views
        print(f"  ID={vid_id} | Views={views} | Likes={likes} | URL={v.get('permalink_url','N/A')}")

    # Step 3: get posts and match views
    posts_r = _req.get(f"{BASE}/me/posts", params={
        "fields": "id,message,created_time,permalink_url,likes.summary(true),comments.summary(true),shares",
        "limit": 10,
        "access_token": TOKEN
    }, timeout=20)
    posts = posts_r.json().get("data", [])

    print(f"\n=== Posts from /me/posts (merged with video views) ===")
    for p in posts:
        pid = p.get("id","")
        sub_id = pid.split("_")[-1] if "_" in pid else pid
        views = video_views_map.get(sub_id, 0)
        likes = p.get("likes",{}).get("summary",{}).get("total_count",0)
        permalink = p.get("permalink_url","")
        is_reel = "/reel/" in permalink or "/videos/" in permalink
        msg = (p.get("message","") or "")[:40]
        print(f"  ID={sub_id} | Views={views} | Likes={likes} | IsReel={is_reel} | Text={msg!r}")

    print(f"\nSummary: {len(vids)} videos with views data found.")
    print(f"Video views map: {video_views_map}")

if __name__ == "__main__":
    main()
