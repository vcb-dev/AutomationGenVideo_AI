"""
Daily sync service — chạy 1 lần/ngày qua Celery cron.
Fetch data từ TikTok (Apify), Facebook, YouTube, Instagram
rồi lưu vào channel_post_stats, channel_monthly_stats, ads_campaign_stats.
"""
import os
import logging
import calendar
import requests
import psycopg2
import psycopg2.extras
from datetime import date, datetime
from django.conf import settings

logger = logging.getLogger(__name__)


# ─── DB helpers ──────────────────────────────────────────────────────────────

def _env(key):
    return str(getattr(settings, key, '') or os.getenv(key, '')).strip()


def _db_conn():
    url = _env('DIRECT_URL') or _env('DATABASE_URL').replace('?pgbouncer=true', '')
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _db_query(sql, params=None):
    try:
        conn = _db_conn()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"DB query error: {e}")
        return []


def _db_execute(sql, params=None):
    try:
        conn = _db_conn()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"DB execute error: {e}")
        return False


def _upsert_post_stat(platform, channel_name, username, team, owner,
                      post_id, title, url, published_at, views, likes, comments, shares,
                      year, month):
    sql = """
        INSERT INTO channel_post_stats
          (id, platform, channel_name, username, team, owner,
           post_id, title, url, published_at, views, likes, comments, shares,
           year, month, synced_at)
        VALUES
          (gen_random_uuid(), %s, %s, %s, %s, %s,
           %s, %s, %s, %s, %s, %s, %s, %s,
           %s, %s, NOW())
        ON CONFLICT (platform, post_id) DO UPDATE SET
          views = EXCLUDED.views, likes = EXCLUDED.likes,
          comments = EXCLUDED.comments, shares = EXCLUDED.shares,
          synced_at = NOW()
    """
    _db_execute(sql, (platform, channel_name, username, team, owner,
                      post_id, title, url, published_at, views, likes, comments, shares,
                      year, month))


def _upsert_monthly_stat(platform, channel_name, username, team, owner,
                         year, month, views, likes, comments, shares, followers, post_count):
    sql = """
        INSERT INTO channel_monthly_stats
          (id, platform, channel_name, username, team, owner,
           year, month, total_views, total_likes, total_comments,
           total_shares, total_followers, post_count, synced_at)
        VALUES
          (gen_random_uuid(), %s, %s, %s, %s, %s,
           %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (platform, username, year, month) DO UPDATE SET
          total_views = EXCLUDED.total_views,
          total_likes = EXCLUDED.total_likes,
          total_comments = EXCLUDED.total_comments,
          total_shares = EXCLUDED.total_shares,
          total_followers = EXCLUDED.total_followers,
          post_count = EXCLUDED.post_count,
          synced_at = NOW()
    """
    _db_execute(sql, (platform, channel_name, username, team, owner,
                      year, month, views, likes, comments, shares, followers, post_count))


def _upsert_ads_stat(account_id, account_name, campaign_id, campaign_name,
                     camp_type, content_type, spend, impressions, reach,
                     mess, cost_mess, likes, cost_like, engagements, cost_eng,
                     clicks, date_start, date_stop, year, month):
    sql = """
        INSERT INTO ads_campaign_stats
          (id, account_id, account_name, campaign_id, campaign_name,
           camp_type, content_type, spend, impressions, reach,
           mess_count, cost_per_mess, like_count, cost_per_like,
           engagement_count, cost_per_engagement, clicks,
           date_start, date_stop, year, month, synced_at)
        VALUES
          (gen_random_uuid(), %s, %s, %s, %s,
           %s, %s, %s, %s, %s,
           %s, %s, %s, %s,
           %s, %s, %s,
           %s, %s, %s, %s, NOW())
        ON CONFLICT (campaign_id, date_start, date_stop) DO UPDATE SET
          spend = EXCLUDED.spend, impressions = EXCLUDED.impressions,
          mess_count = EXCLUDED.mess_count, cost_per_mess = EXCLUDED.cost_per_mess,
          like_count = EXCLUDED.like_count, cost_per_like = EXCLUDED.cost_per_like,
          engagement_count = EXCLUDED.engagement_count,
          cost_per_engagement = EXCLUDED.cost_per_engagement,
          synced_at = NOW()
    """
    _db_execute(sql, (account_id, account_name, campaign_id, campaign_name,
                      camp_type, content_type, spend, impressions, reach,
                      mess, cost_mess, likes, cost_like, engagements, cost_eng,
                      clicks, date_start, date_stop, year, month))


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _month_range(year, month):
    last = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last}"


def _get_video_date(item):
    iso = item.get("createTimeISO") or ""
    ts  = item.get("createTime", 0)
    if iso: return iso[:10]
    if ts: return datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
    return ""


def _get_channels(platform_like):
    return _db_query("""
        SELECT name, channel_id, link_channel, team_traffic AS team, owner
        FROM huyk_channels
        WHERE LOWER(platform) LIKE %s AND status IN ('Đang hoạt động', 'ON')
        ORDER BY name
    """, [f"%{platform_like}%"])


def _extract_uid(channel_id, link, prefix=""):
    uid = (channel_id or "").strip().strip("\n").lstrip(prefix)
    if uid: return uid
    if prefix == "@" and "tiktok.com/@" in (link or ""):
        return link.split("tiktok.com/@")[-1].split("?")[0].strip()
    if "facebook.com" in (link or ""):
        import re
        m = re.search(r'profile\.php\?id=(\d+)', link)
        if m: return m.group(1)
        m = re.search(r'/people/[^/]+/(\d+)', link)
        if m: return m.group(1)
        m = re.search(r'facebook\.com/([^/?#\s]+)', link)
        if m:
            s = m.group(1).rstrip('/')
            if s not in ('share', 'pages', 'groups'): return s
    return ""


# ─── TikTok sync ─────────────────────────────────────────────────────────────

def sync_tiktok(year: int, month: int):
    apify_token = _env('APIFY_API_TOKEN')
    if not apify_token or 'placeholder' in apify_token:
        logger.warning("TikTok sync skipped: no APIFY_API_TOKEN")
        return 0

    date_from, date_to = _month_range(year, month)
    channels = _get_channels("tiktok")
    synced = 0

    for ch in channels:
        uid = _extract_uid(ch.get("channel_id",""), ch.get("link_channel",""), "@")
        if not uid: continue

        try:
            r = requests.post(
                "https://api.apify.com/v2/acts/clockworks~free-tiktok-scraper/run-sync-get-dataset-items",
                params={"token": apify_token, "timeout": 120},
                json={"profiles": [uid], "resultsPerPage": 30},
                timeout=150,
            )
            if not r.ok: continue
            items = r.json() or []

            total = {"views":0,"likes":0,"comments":0,"shares":0,"posts":0}
            for item in items:
                pub = _get_video_date(item)
                if not pub: continue
                if pub < date_from: break
                if pub > date_to: continue

                v = int(item.get("playCount",0))
                l = int(item.get("diggCount",0))
                c = int(item.get("commentCount",0))
                s = int(item.get("shareCount",0))

                _upsert_post_stat(
                    "tiktok", ch["name"], uid, ch.get("team",""), ch.get("owner",""),
                    item.get("id",""), (item.get("text") or "")[:200],
                    item.get("webVideoUrl",""), pub, v, l, c, s, year, month
                )
                total["views"] += v; total["likes"] += l
                total["comments"] += c; total["shares"] += s; total["posts"] += 1

            _upsert_monthly_stat(
                "tiktok", ch["name"], uid, ch.get("team",""), ch.get("owner",""),
                year, month, total["views"], total["likes"],
                total["comments"], total["shares"], 0, total["posts"]
            )
            synced += 1
            logger.info(f"TikTok synced: {uid} → {total['posts']} videos, {total['views']:,} views")
        except Exception as e:
            logger.error(f"TikTok sync error {uid}: {e}")

    return synced


# ─── Facebook sync ────────────────────────────────────────────────────────────

def sync_facebook(year: int, month: int):
    token = _env('FACEBOOK_ACCESS_TOKEN')
    if not token: return 0

    date_from, date_to = _month_range(year, month)
    since_ts = int(datetime.strptime(date_from, "%Y-%m-%d").timestamp())
    until_ts = int(datetime.strptime(date_to,   "%Y-%m-%d").timestamp()) + 86399
    channels = _get_channels("facebook")
    synced = 0

    for ch in channels:
        uid = _extract_uid(ch.get("channel_id",""), ch.get("link_channel",""))
        if not uid: continue

        try:
            page_r = requests.get(f"https://graph.facebook.com/v19.0/{uid}", params={
                "access_token": token,
                "fields": "id,name,access_token,followers_count",
            }, timeout=10)
            if not page_r.ok: continue
            pd = page_r.json()
            if "error" in pd: continue

            page_token = pd.get("access_token", token)
            followers  = pd.get("followers_count", 0)

            posts_r = requests.get(f"https://graph.facebook.com/v19.0/{pd['id']}/posts", params={
                "access_token": page_token,
                "fields": "id,message,created_time,likes.summary(true),comments.summary(true),shares",
                "since": since_ts, "until": until_ts, "limit": 50,
            }, timeout=15)
            if not posts_r.ok: continue
            posts = posts_r.json().get("data", [])

            total = {"views":0,"likes":0,"comments":0,"shares":0,"posts":len(posts)}
            for post in posts:
                likes    = post.get("likes",{}).get("summary",{}).get("total_count",0)
                comments = post.get("comments",{}).get("summary",{}).get("total_count",0)
                shares   = post.get("shares",{}).get("count",0)
                total["likes"] += likes; total["comments"] += comments; total["shares"] += shares

                _upsert_post_stat(
                    "facebook", ch["name"], uid, ch.get("team",""), ch.get("owner",""),
                    post["id"], (post.get("message") or "")[:200], "",
                    post.get("created_time","")[:10], 0, likes, comments, shares, year, month
                )

            _upsert_monthly_stat(
                "facebook", ch["name"], uid, ch.get("team",""), ch.get("owner",""),
                year, month, 0, total["likes"], total["comments"],
                total["shares"], followers, total["posts"]
            )
            synced += 1
        except Exception as e:
            logger.error(f"Facebook sync error {uid}: {e}")

    return synced


# ─── YouTube sync ─────────────────────────────────────────────────────────────

def sync_youtube(year: int, month: int):
    yt_key = _env('YOUTUBE_API_KEY')
    if not yt_key: return 0

    date_from, date_to = _month_range(year, month)
    channels = _get_channels("youtube")
    synced = 0

    for ch in channels:
        uid = _extract_uid(ch.get("channel_id",""), ch.get("link_channel",""))
        uid = uid.strip().strip("\n")
        if not uid: continue

        try:
            # Resolve channel ID
            channel_id = uid if uid.startswith("UC") else None
            if not channel_id:
                handle = uid.lstrip("@")
                r = requests.get("https://www.googleapis.com/youtube/v3/channels", params={
                    "key": yt_key, "forHandle": f"@{handle}", "part": "id,statistics",
                }, timeout=10)
                items = r.json().get("items", []) if r.ok else []
                if items:
                    channel_id = items[0]["id"]

            if not channel_id: continue

            # Search videos in month
            sr = requests.get("https://www.googleapis.com/youtube/v3/search", params={
                "key": yt_key, "channelId": channel_id, "part": "id",
                "type": "video", "order": "date",
                "publishedAfter": f"{date_from}T00:00:00Z",
                "publishedBefore": f"{date_to}T23:59:59Z",
                "maxResults": 50,
            }, timeout=15)
            if not sr.ok: continue
            video_ids = [i["id"]["videoId"] for i in sr.json().get("items", [])]
            if not video_ids: continue

            vr = requests.get("https://www.googleapis.com/youtube/v3/videos", params={
                "key": yt_key, "id": ",".join(video_ids), "part": "snippet,statistics",
            }, timeout=15)
            if not vr.ok: continue

            total = {"views":0,"likes":0,"comments":0,"posts":0}
            for item in vr.json().get("items", []):
                s = item.get("statistics", {})
                v = int(s.get("viewCount",0)); l = int(s.get("likeCount",0))
                c = int(s.get("commentCount",0))
                pub = item["snippet"]["publishedAt"][:10]

                _upsert_post_stat(
                    "youtube", ch["name"], uid, ch.get("team",""), ch.get("owner",""),
                    item["id"], item["snippet"]["title"][:200], "", pub, v, l, c, 0, year, month
                )
                total["views"] += v; total["likes"] += l; total["comments"] += c; total["posts"] += 1

            _upsert_monthly_stat(
                "youtube", ch["name"], uid, ch.get("team",""), ch.get("owner",""),
                year, month, total["views"], total["likes"], total["comments"], 0, 0, total["posts"]
            )
            synced += 1
        except Exception as e:
            logger.error(f"YouTube sync error {uid}: {e}")

    return synced


# ─── Instagram sync ──────────────────────────────────────────────────────────

def sync_instagram(year: int, month: int):
    token = _env('FACEBOOK_ACCESS_TOKEN')
    if not token: return 0

    date_from, date_to = _month_range(year, month)
    since_ts = int(datetime.strptime(date_from, "%Y-%m-%d").timestamp())
    until_ts = int(datetime.strptime(date_to,   "%Y-%m-%d").timestamp()) + 86399

    # Lấy tất cả IG accounts từ FB pages
    ig_accounts = []
    url = "https://graph.facebook.com/v19.0/me/accounts"
    while url:
        r = requests.get(url, params={
            "access_token": token,
            "fields": "id,name,instagram_business_account{id,username,followers_count}",
            "limit": 50,
        }, timeout=15)
        if not r.ok: break
        data = r.json()
        for p in data.get("data", []):
            ig = p.get("instagram_business_account")
            if ig:
                ig_accounts.append({
                    "ig_id": ig["id"], "username": ig.get("username",""),
                    "followers": ig.get("followers_count",0), "page_name": p["name"],
                })
        url = data.get("paging", {}).get("next")

    synced = 0
    for acc in ig_accounts:
        try:
            media_r = requests.get(f"https://graph.facebook.com/v19.0/{acc['ig_id']}/media", params={
                "access_token": token,
                "fields": "id,caption,media_type,timestamp,like_count,comments_count",
                "since": since_ts, "until": until_ts, "limit": 50,
            }, timeout=15)
            if not media_r.ok: continue
            items = media_r.json().get("data", [])

            total = {"likes":0,"comments":0,"posts":len(items)}
            for item in items:
                l = item.get("like_count",0); c = item.get("comments_count",0)
                total["likes"] += l; total["comments"] += c
                pub = item.get("timestamp","")[:10]
                _upsert_post_stat(
                    "instagram", acc["page_name"], acc["username"], "", "",
                    item["id"], (item.get("caption") or "")[:200], "",
                    pub, 0, l, c, 0, year, month
                )

            _upsert_monthly_stat(
                "instagram", acc["page_name"], acc["username"], "", "",
                year, month, 0, total["likes"], total["comments"],
                0, acc["followers"], total["posts"]
            )
            synced += 1
        except Exception as e:
            logger.error(f"Instagram sync error {acc['username']}: {e}")

    return synced


# ─── Facebook Ads sync ────────────────────────────────────────────────────────

def sync_fb_ads(year: int, month: int):
    token = _env('FACEBOOK_ACCESS_TOKEN')
    if not token: return 0

    date_from, date_to = _month_range(year, month)

    # Lấy ad accounts
    r = requests.get("https://graph.facebook.com/v19.0/me/adaccounts", params={
        "access_token": token, "fields": "id,name", "limit": 20,
    }, timeout=15)
    if not r.ok: return 0
    accounts = r.json().get("data", [])
    synced = 0

    for acc in accounts:
        acc_id = acc["id"]
        try:
            ins_r = requests.get(f"https://graph.facebook.com/v19.0/{acc_id}/insights", params={
                "access_token": token,
                "fields": "campaign_id,campaign_name,spend,impressions,reach,clicks,actions,cost_per_action_type",
                "date_preset": "this_month" if year == date.today().year and month == date.today().month else "last_month",
                "level": "campaign", "limit": 100,
            }, timeout=30)
            if not ins_r.ok: continue
            rows = ins_r.json().get("data", [])

            for row in rows:
                actions  = {a["action_type"]: int(float(a["value"])) for a in row.get("actions",[])}
                cost_per = {a["action_type"]: float(a["value"]) for a in row.get("cost_per_action_type",[])}
                spend    = float(row.get("spend",0))

                mess     = actions.get("onsite_conversion.messaging_conversation_started_7d",0)
                cost_m   = cost_per.get("onsite_conversion.messaging_conversation_started_7d",0)
                if mess > 0 and cost_m == 0: cost_m = spend / mess

                likes    = actions.get("like",0)
                cost_l   = cost_per.get("like",0)
                engage   = actions.get("post_engagement",0)
                cost_e   = cost_per.get("post_engagement",0)

                name = row.get("campaign_name","")
                nl = name.lower()
                camp_type = ("mess" if "mess" in nl else
                             "like_page" if "like page" in nl else
                             "tuong_tac" if "tương tác" in nl else "other")
                ct = "unknown"
                for c in ["A5","A4","A3","A2","A1"]:
                    if f"- {c}" in name or f"- {c} -" in name: ct = c; break

                _upsert_ads_stat(
                    acc_id, acc.get("name",""), row.get("campaign_id",""), name,
                    camp_type, ct, spend,
                    int(row.get("impressions",0)), int(row.get("reach",0)),
                    mess, cost_m, likes, cost_l, engage, cost_e,
                    int(row.get("clicks",0)),
                    row.get("date_start", date_from), row.get("date_stop", date_to),
                    year, month
                )
                synced += 1
        except Exception as e:
            logger.error(f"FB Ads sync error {acc_id}: {e}")

    return synced


# ─── Master sync ─────────────────────────────────────────────────────────────

def run_daily_sync(year: int = None, month: int = None):
    """Entry point — gọi từ Celery task hoặc thủ công."""
    today = date.today()
    year  = year  or today.year
    month = month or today.month

    logger.info(f"=== Daily sync start: {year}-{month:02d} ===")
    results = {}

    results["tiktok"]    = sync_tiktok(year, month)
    results["facebook"]  = sync_facebook(year, month)
    results["youtube"]   = sync_youtube(year, month)
    results["instagram"] = sync_instagram(year, month)
    results["fb_ads"]    = sync_fb_ads(year, month)

    logger.info(f"=== Daily sync done: {results} ===")
    return results
