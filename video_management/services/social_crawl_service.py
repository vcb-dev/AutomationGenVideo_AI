"""
Social crawl service — crawl TikTok, YouTube, Facebook, Instagram
rồi lưu vào social_video_report.
Được gọi từ Celery task mỗi ngày lúc 2:00 AM.
"""
import os, re, time, logging, calendar, requests, psycopg2, psycopg2.extras
from datetime import date, datetime
from django.conf import settings

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
def _env(k): return str(getattr(settings, k, '') or os.getenv(k, '')).strip()

APIFY_TOKEN  = lambda: _env('APIFY_API_TOKEN')
FB_TOKEN     = lambda: _env('FACEBOOK_ACCESS_TOKEN')
YT_KEY       = lambda: _env('YOUTUBE_API_KEY')
FB_BASE      = "https://graph.facebook.com/v19.0"
YT_BASE      = "https://www.googleapis.com/youtube/v3"
APIFY_ACTOR  = "clockworks~free-tiktok-scraper"

# ── DB ────────────────────────────────────────────────────────────────────────
def _db_conn():
    url = _env('DIRECT_URL') or _env('DATABASE_URL').replace('?pgbouncer=true', '')
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)

def _db_query(sql, params=None):
    try:
        conn = _db_conn()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"DB query error: {e}")
        return []

def _upsert_posts(posts: list) -> int:
    if not posts: return 0
    conn = _db_conn()
    saved = 0
    try:
        with conn.cursor() as cur:
            for p in posts:
                try:
                    cur.execute("""
                        INSERT INTO social_video_report
                          (id, platform, post_id, channel_name, username, owner, team,
                           title, hashtags, views, likes, comments, shares,
                           followers, video_url, published_at, year, month, source, synced_at)
                        VALUES
                          (gen_random_uuid(), %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s,
                           %s, %s, %s::date, %s, %s, %s, NOW())
                        ON CONFLICT (platform, post_id) DO UPDATE SET
                          views=EXCLUDED.views, likes=EXCLUDED.likes,
                          comments=EXCLUDED.comments, shares=EXCLUDED.shares,
                          followers=EXCLUDED.followers, hashtags=EXCLUDED.hashtags,
                          title=EXCLUDED.title, synced_at=NOW()
                    """, (
                        p['platform'], p['post_id'], p['channel_name'], p['username'],
                        p.get('owner',''), p.get('team',''),
                        p.get('title','')[:500], p.get('hashtags',[]),
                        p.get('views',0), p.get('likes',0),
                        p.get('comments',0), p.get('shares',0),
                        p.get('followers',0), p.get('url',''),
                        p.get('published_at') or None,
                        p['year'], p['month'], p.get('source','api')
                    ))
                    saved += 1
                except Exception as e:
                    logger.warning(f"Skip post {p.get('post_id','?')}: {e}")
        conn.commit()
        return saved
    except Exception as e:
        conn.rollback()
        logger.error(f"Upsert error: {e}")
        return 0
    finally:
        conn.close()

# ── Helpers ───────────────────────────────────────────────────────────────────
def _month_range(year, month):
    last = calendar.monthrange(year, month)[1]
    today = date.today()
    date_to = today.strftime('%Y-%m-%d') if (year == today.year and month == today.month) \
              else f"{year}-{month:02d}-{last}"
    return f"{year}-{month:02d}-01", date_to

def _extract_uid(channel_id, link, platform=''):
    uid = (channel_id or '').strip().strip('\n').lstrip('@')
    if uid: return uid
    link = link or ''
    if 'tiktok.com/@' in link:
        return link.split('tiktok.com/@')[-1].split('?')[0].strip('/')
    if 'facebook.com' in link:
        m = re.search(r'profile\.php\?id=(\d+)', link)
        if m: return m.group(1)
        m = re.search(r'/people/[^/]+/(\d+)', link)
        if m: return m.group(1)
        m = re.search(r'facebook\.com/([^/?#\s]+)', link)
        if m:
            s = m.group(1).rstrip('/')
            if s not in ('share','pages','groups'): return s
    if 'youtube.com' in link:
        m = re.search(r'youtube\.com/(@[^/?#\s]+|channel/([^/?#\s]+))', link)
        if m: return m.group(1)
    if 'instagram.com' in link:
        m = re.search(r'instagram\.com/([^/?#\s]+)', link)
        if m: return m.group(1).rstrip('/')
    return ''

def _extract_hashtags(text):
    return re.findall(r'#\w+', text or '')

def _get_channels(platform_like):
    return _db_query("""
        SELECT name, channel_id, link_channel, team_traffic AS team, owner
        FROM huyk_channels
        WHERE LOWER(platform) LIKE %s
          AND status IN ('Đang hoạt động', 'ON')
        ORDER BY name
    """, [f"%{platform_like}%"])

def _get_ig_biz_accounts():
    token = FB_TOKEN()
    if not token: return {}
    accounts = {}
    try:
        url = f"{FB_BASE}/me/accounts"
        while url:
            r = requests.get(url, params={
                "access_token": token,
                "fields": "id,name,instagram_business_account{id,username,followers_count}",
                "limit": 50,
            }, timeout=15)
            if not r.ok: break
            for p in r.json().get("data", []):
                ig = p.get("instagram_business_account")
                if ig:
                    accounts[ig.get("username","").lower()] = {
                        "ig_id": ig["id"],
                        "username": ig.get("username",""),
                        "followers": ig.get("followers_count", 0),
                    }
            url = r.json().get("paging", {}).get("next")
    except Exception as e:
        logger.error(f"IG biz accounts error: {e}")
    return accounts

# ── TikTok ────────────────────────────────────────────────────────────────────
def crawl_tiktok(year: int, month: int) -> dict:
    token = APIFY_TOKEN()
    if not token or token == 'your_apify_api_token_here':
        return {"skipped": "No APIFY_API_TOKEN"}

    date_from, date_to = _month_range(year, month)
    channels = _get_channels('tiktok')
    total_saved, total_channels = 0, 0

    for ch in channels:
        uid = _extract_uid(ch.get('channel_id',''), ch.get('link_channel',''))
        if not uid: continue
        try:
            r = requests.post(
                f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
                params={"token": token, "timeout": 120},
                json={"profiles": [uid], "resultsPerPage": 30},
                timeout=150,
            )
            if not r.ok:
                logger.warning(f"TikTok Apify error {uid}: {r.status_code}")
                continue
            posts = []
            for item in (r.json() or []):
                iso = (item.get("createTimeISO") or "")[:10]
                ts  = item.get("createTime", 0)
                if not iso and ts:
                    iso = datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
                if not iso: continue
                if iso < date_from: break
                if iso > date_to:   continue
                posts.append({
                    "platform": "tiktok", "post_id": item.get("id",""),
                    "channel_name": ch["name"], "username": uid,
                    "owner": ch.get("owner",""), "team": ch.get("team",""),
                    "title": (item.get("text") or "")[:500],
                    "hashtags": _extract_hashtags(item.get("text","")),
                    "views": int(item.get("playCount",0)),
                    "likes": int(item.get("diggCount",0)),
                    "comments": int(item.get("commentCount",0)),
                    "shares": int(item.get("shareCount",0)),
                    "followers": int(item.get("authorMeta",{}).get("fans",0)),
                    "url": item.get("webVideoUrl",""),
                    "published_at": iso, "year": year, "month": month, "source": "apify",
                })
            n = _upsert_posts(posts)
            total_saved += n
            total_channels += 1
            logger.info(f"TikTok @{uid}: {len(posts)} videos, {n} saved")
            time.sleep(1)
        except Exception as e:
            logger.error(f"TikTok crawl error {uid}: {e}")

    return {"channels": total_channels, "saved": total_saved}

# ── YouTube ───────────────────────────────────────────────────────────────────
def crawl_youtube(year: int, month: int) -> dict:
    yt_key = YT_KEY()
    if not yt_key: return {"skipped": "No YOUTUBE_API_KEY"}

    date_from, date_to = _month_range(year, month)
    channels = _get_channels('youtube')
    total_saved, total_channels = 0, 0

    for ch in channels:
        uid = _extract_uid(ch.get('channel_id',''), ch.get('link_channel',''))
        uid = uid.strip().strip('\n')
        if not uid: continue
        try:
            channel_id = uid if uid.startswith("UC") else None
            if not channel_id:
                handle = uid.lstrip("@")
                r = requests.get(f"{YT_BASE}/channels", params={
                    "key": yt_key, "forHandle": f"@{handle}", "part": "id,statistics"}, timeout=10)
                items = r.json().get("items",[]) if r.ok else []
                if not items:
                    r2 = requests.get(f"{YT_BASE}/channels", params={
                        "key": yt_key, "forUsername": handle, "part": "id,statistics"}, timeout=10)
                    items = r2.json().get("items",[]) if r2.ok else []
                if items: channel_id = items[0]["id"]
            if not channel_id: continue

            followers = 0
            ch_r = requests.get(f"{YT_BASE}/channels", params={
                "key": yt_key, "id": channel_id, "part": "statistics"}, timeout=10)
            if ch_r.ok:
                followers = int(ch_r.json().get("items",[{}])[0].get("statistics",{}).get("subscriberCount",0))

            sr = requests.get(f"{YT_BASE}/search", params={
                "key": yt_key, "channelId": channel_id, "part": "id",
                "type": "video", "order": "date", "maxResults": 50,
                "publishedAfter": f"{date_from}T00:00:00Z",
                "publishedBefore": f"{date_to}T23:59:59Z",
            }, timeout=15)
            video_ids = [i["id"]["videoId"] for i in (sr.json().get("items",[]) if sr.ok else [])]
            if not video_ids: continue

            vr = requests.get(f"{YT_BASE}/videos", params={
                "key": yt_key, "id": ",".join(video_ids), "part": "snippet,statistics"}, timeout=15)
            posts = []
            for item in (vr.json().get("items",[]) if vr.ok else []):
                s       = item.get("statistics",{})
                snippet = item.get("snippet",{})
                desc    = snippet.get("description","") or ""
                title   = snippet.get("title","") or ""
                # Hashtag từ description + title (YouTube nhúng #tag trong mô tả)
                # Fallback sang tags nếu là video của chính mình
                tags_meta = [f"#{t}" for t in snippet.get("tags",[])[:5]]
                tags_desc = _extract_hashtags(desc)[:10]
                tags_title = _extract_hashtags(title)
                hashtags  = list(dict.fromkeys(tags_desc + tags_title + tags_meta))[:15]
                posts.append({
                    "platform": "youtube", "post_id": item["id"],
                    "channel_name": ch["name"], "username": uid,
                    "owner": ch.get("owner",""), "team": ch.get("team",""),
                    "title": title[:500],
                    "hashtags": hashtags,
                    "views": int(s.get("viewCount",0)),
                    "likes": int(s.get("likeCount",0)),
                    "comments": int(s.get("commentCount",0)),
                    "shares": 0, "followers": followers,
                    "url": f"https://youtube.com/watch?v={item['id']}",
                    "published_at": item["snippet"]["publishedAt"][:10],
                    "year": year, "month": month, "source": "api",
                })
            n = _upsert_posts(posts)
            total_saved += n
            total_channels += 1
            logger.info(f"YouTube @{uid}: {len(posts)} videos, {n} saved")
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"YouTube crawl error {uid}: {e}")

    return {"channels": total_channels, "saved": total_saved}

# ── Facebook ──────────────────────────────────────────────────────────────────
def crawl_facebook(year: int, month: int) -> dict:
    token = FB_TOKEN()
    if not token: return {"skipped": "No FACEBOOK_ACCESS_TOKEN"}

    date_from, date_to = _month_range(year, month)
    since_ts = int(datetime.strptime(date_from, "%Y-%m-%d").timestamp())
    until_ts = int(datetime.strptime(date_to,   "%Y-%m-%d").timestamp()) + 86399
    channels = _get_channels('facebook')
    total_saved, total_channels = 0, 0

    for ch in channels:
        uid = _extract_uid(ch.get('channel_id',''), ch.get('link_channel',''))
        if not uid: continue
        try:
            page_r = requests.get(f"{FB_BASE}/{uid}", params={
                "access_token": token, "fields": "id,name,access_token,followers_count"}, timeout=10)
            if not page_r.ok: continue
            pd = page_r.json()
            if "error" in pd: continue
            page_token = pd.get("access_token", token)
            page_id    = pd.get("id", uid)
            followers  = pd.get("followers_count", 0)

            posts_r = requests.get(f"{FB_BASE}/{page_id}/posts", params={
                "access_token": page_token,
                "fields": "id,message,created_time,likes.summary(true),comments.summary(true),shares",
                "since": since_ts, "until": until_ts, "limit": 50}, timeout=15)
            if not posts_r.ok: continue

            posts = []
            for post in posts_r.json().get("data",[]):
                msg      = post.get("message","") or ""
                likes    = post.get("likes",{}).get("summary",{}).get("total_count",0)
                comments = post.get("comments",{}).get("summary",{}).get("total_count",0)
                shares   = post.get("shares",{}).get("count",0)
                posts.append({
                    "platform": "facebook", "post_id": post["id"],
                    "channel_name": ch["name"], "username": uid,
                    "owner": ch.get("owner",""), "team": ch.get("team",""),
                    "title": msg[:500], "hashtags": _extract_hashtags(msg),
                    "views": 0, "likes": likes, "comments": comments, "shares": shares,
                    "followers": followers, "url": f"https://facebook.com/{post['id']}",
                    "published_at": post.get("created_time","")[:10],
                    "year": year, "month": month, "source": "api",
                })
            n = _upsert_posts(posts)
            total_saved += n
            total_channels += 1
            logger.info(f"Facebook @{uid}: {len(posts)} posts, {n} saved")
            time.sleep(1)
        except Exception as e:
            logger.error(f"Facebook crawl error {uid}: {e}")

    return {"channels": total_channels, "saved": total_saved}

# ── Instagram ─────────────────────────────────────────────────────────────────
def crawl_instagram(year: int, month: int) -> dict:
    token = FB_TOKEN()
    if not token: return {"skipped": "No FACEBOOK_ACCESS_TOKEN"}

    date_from, date_to = _month_range(year, month)
    since_ts = int(datetime.strptime(date_from, "%Y-%m-%d").timestamp())
    until_ts = int(datetime.strptime(date_to,   "%Y-%m-%d").timestamp()) + 86399
    ig_biz   = _get_ig_biz_accounts()
    total_saved, total_channels = 0, 0

    for username, acc in ig_biz.items():
        try:
            media_r = requests.get(f"{FB_BASE}/{acc['ig_id']}/media", params={
                "access_token": token,
                "fields": "id,caption,media_type,timestamp,like_count,comments_count,video_views",
                "since": since_ts, "until": until_ts, "limit": 50}, timeout=15)
            if not media_r.ok: continue

            posts = []
            for item in media_r.json().get("data",[]):
                cap        = item.get("caption","") or ""
                pub        = item.get("timestamp","")[:10]
                media_type = item.get("media_type","")
                if not pub or not (date_from <= pub <= date_to): continue

                # video_views chỉ có với VIDEO và REEL, IMAGE = 0
                views = int(item.get("video_views", 0) or 0)

                posts.append({
                    "platform": "instagram", "post_id": item["id"],
                    "channel_name": acc.get("username", username),
                    "username": username,
                    "owner": "", "team": "",
                    "title": cap[:500], "hashtags": _extract_hashtags(cap),
                    "views": views, "likes": int(item.get("like_count",0)),
                    "comments": int(item.get("comments_count",0)), "shares": 0,
                    "followers": acc.get("followers",0),
                    "url": f"https://instagram.com/p/{item['id']}",
                    "published_at": pub, "year": year, "month": month, "source": "api",
                })
            n = _upsert_posts(posts)
            total_saved += n
            total_channels += 1
            logger.info(f"Instagram @{username}: {len(posts)} posts, {n} saved")
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"Instagram crawl error {username}: {e}")

    return {"channels": total_channels, "saved": total_saved}

# ── Master crawl ──────────────────────────────────────────────────────────────
def run_social_crawl(year: int = None, month: int = None) -> dict:
    today = date.today()
    year  = year  or today.year
    month = month or today.month

    logger.info(f"=== Social crawl start: {year}-{month:02d} ===")

    results = {
        "tiktok":    crawl_tiktok(year, month),
        "youtube":   crawl_youtube(year, month),
        "facebook":  crawl_facebook(year, month),
        "instagram": crawl_instagram(year, month),
    }

    total = sum(r.get("saved", 0) for r in results.values() if isinstance(r, dict))
    logger.info(f"=== Social crawl done: {results} | total_saved={total} ===")
    return {"year": year, "month": month, "results": results, "total_saved": total}
