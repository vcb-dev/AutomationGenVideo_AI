"""
Script kiem tra Meta Graph API xem co lay duoc du lieu views/video khong.
Run: python check_graph_views.py
"""

import requests
import json
import sys

TOKEN = "EAASfB9OySdwBRN9lm5WpzpLMKoTG69JYdvZANXeWV5kAXVZBtJmBwycwZCRxNC0gglbW1XZBumAmFiUhJiQllv6eqiW8MDjOkyq60gvKJzYUrVqEmx15GQ936HxkAq7gdymIUMcUHNwislK0jmWbjcV1Qdou1oBxLzUZBlFlCiOL6KzDfzqVhITMAZBwdVRwZC4ZAhTPDp4YS3LTZAoQSm3PCDJrNZCR9dX3ReVDVBZBaieZCTMZD"
BASE = "https://graph.facebook.com/v20.0"

def _get(path, params=None):
    p = {"access_token": TOKEN}
    if params:
        p.update(params)
    r = requests.get(f"{BASE}{path}", params=p, timeout=20)
    return r.status_code, r.json()

def s(text):
    """Safe print - handle unicode on windows console."""
    return text.encode('ascii', errors='replace').decode('ascii')

def p(text):
    print(s(text))

def check_token_info():
    print("=" * 60)
    print("1. TOKEN INFO (/me)")
    print("=" * 60)
    status, data = _get("/me", {"fields": "id,name,category,fan_count,followers_count"})
    print(f"HTTP {status}: {json.dumps(data, indent=2, ensure_ascii=True)}\n")
    return data.get("id"), data.get("name")

def check_posts(page_id):
    print("=" * 60)
    print("2. POSTS - likes, comments, shares (basic)")
    print("=" * 60)
    status, data = _get(f"/{page_id}/posts", {
        "fields": "id,message,created_time,permalink_url,likes.summary(true),comments.summary(true),shares",
        "limit": 2
    })
    print(f"HTTP {status}")
    for post in data.get("data", []):
        likes = post.get("likes", {}).get("summary", {}).get("total_count", "N/A")
        comments = post.get("comments", {}).get("summary", {}).get("total_count", "N/A")
        shares = post.get("shares", {}).get("count", "N/A")
        print(f"  Post ID: {post.get('id')}")
        print(f"    Likes: {likes} | Comments: {comments} | Shares: {shares}")
    if "error" in data:
        print(f"  ERROR: {data['error'].get('message')}")
    print()

def check_videos(page_id):
    print("=" * 60)
    print("3. PAGE VIDEOS - /{page_id}/videos (with views field)")
    print("=" * 60)
    status, data = _get(f"/{page_id}/videos", {
        "fields": "id,title,description,created_time,permalink_url,likes.summary(true),comments.summary(true),views",
        "limit": 3
    })
    print(f"HTTP {status}")
    for vid in data.get("data", []):
        views = vid.get("views", "NOT_RETURNED")
        likes = vid.get("likes", {}).get("summary", {}).get("total_count", "N/A")
        print(f"  Video ID: {vid.get('id')}")
        print(f"    Views: {views} | Likes: {likes}")
        print(f"    URL: {vid.get('permalink_url', 'N/A')}")
    if "error" in data:
        print(f"  ERROR: {data['error'].get('message')}")
    print()
    return data.get("data", [])

def check_video_insights(video_id):
    print("=" * 60)
    print(f"4. VIDEO INSIGHTS for video_id={video_id}")
    print("   (requires Live Mode app + pages_read_engagement)")
    print("=" * 60)
    status, data = _get(f"/{video_id}/video_insights", {
        "metric": "total_video_views,total_video_complete_views,total_video_views_unique",
        "period": "lifetime"
    })
    print(f"HTTP {status}")
    if "data" in data:
        for metric in data["data"]:
            vals = metric.get("values", [{}])
            val = vals[0].get("value", "N/A") if vals else "N/A"
            print(f"  {metric.get('name')}: {val}")
    if "error" in data:
        print(f"  ERROR CODE {data['error'].get('code')}: {data['error'].get('message')}")
    print()

def check_page_insights(page_id):
    print("=" * 60)
    print("5. PAGE INSIGHTS - page_video_views (month period)")
    print("   (requires Live Mode app)")
    print("=" * 60)
    status, data = _get(f"/{page_id}/insights", {
        "metric": "page_video_views,page_video_complete_views_30s,page_views_total",
        "period": "month"
    })
    print(f"HTTP {status}")
    if "data" in data:
        for metric in data["data"]:
            vals = metric.get("values", [])
            latest = vals[-1].get("value", "N/A") if vals else "N/A"
            print(f"  {metric.get('name')}: {latest} (latest value)")
    if "error" in data:
        print(f"  ERROR CODE {data['error'].get('code')}: {data['error'].get('message')}")
    print()

def check_reels_insights(page_id):
    print("=" * 60)
    print("6. REELS via /media or /video_reels")
    print("=" * 60)
    # Try via published_posts with type filter
    status, data = _get(f"/{page_id}/published_posts", {
        "fields": "id,message,permalink_url,likes.summary(true),comments.summary(true),shares",
        "limit": 3
    })
    print(f"HTTP {status} for /published_posts")
    if "error" in data:
        print(f"  ERROR: {data['error'].get('message')}")
    else:
        print(f"  Got {len(data.get('data', []))} published_posts")
    print()

def check_insights_day(page_id):
    print("=" * 60)
    print("7. PAGE INSIGHTS - day period (more granular)")
    print("=" * 60)
    status, data = _get(f"/{page_id}/insights", {
        "metric": "page_video_views,page_impressions_unique",
        "period": "day",
        "limit": 5
    })
    print(f"HTTP {status}")
    if "data" in data:
        for m in data["data"]:
            vals = m.get("values", [])
            val = vals[-1].get("value", "N/A") if vals else "N/A"
            print(f"  {m.get('name')}: {val} (latest)")
    if "error" in data:
        print(f"  ERROR CODE {data['error'].get('code')}: {data['error'].get('message')}")
    print()

if __name__ == "__main__":
    page_id, page_name = check_token_info()
    if not page_id:
        print("ERROR: Cannot get page_id. Token may be expired.")
        sys.exit(1)

    print(f"\nPage: {page_name} (ID: {page_id})\n")

    check_posts(page_id)
    videos = check_videos(page_id)

    if videos:
        vid_id = videos[0].get("id")
        if vid_id:
            check_video_insights(vid_id)

    check_page_insights(page_id)
    check_reels_insights(page_id)
    check_insights_day(page_id)

    print("=" * 60)
    print("KET QUAT PHAN TICH:")
    print("  Token: HOAT DONG (NamblingJewelry page)")
    print("  - Neu /videos tra ve views = 0/None -> App o Development Mode")
    print("  - Neu /video_insights loi 200 thi OK, 400/403 thi can Live Mode")
    print("  - Neu /insights loi (#3) thi can Business Verification + Live Mode")
    print("=" * 60)
