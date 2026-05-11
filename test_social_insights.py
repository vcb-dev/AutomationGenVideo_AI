"""
Crawl insights tất cả kênh MXH (TikTok, YouTube, Facebook, Instagram)
từ bảng huyk_channels → lưu vào social_video_report.

Logic tối ưu chi phí:
  TikTok    → Apify (clockworks scraper)
  YouTube   → YouTube Data API (miễn phí)
  Facebook  → Graph API nếu là admin page (miễn phí), ngược lại Apify
  Instagram → Graph API nếu linked Business account (miễn phí), ngược lại Apify

Chạy:
  python test_social_insights.py --month 5              # tất cả platform
  python test_social_insights.py --month 5 --platform tiktok
  python test_social_insights.py --month 5 --platform youtube
  python test_social_insights.py --month 5 --platform facebook
  python test_social_insights.py --month 5 --platform instagram
  python test_social_insights.py --month 5 --team "Team K1"
  python test_social_insights.py --month 5 --channel huyk.xuongkimhoan2 --platform tiktok
"""

import os, sys, re, json, time, argparse, calendar, requests, psycopg2, psycopg2.extras
from datetime import date, datetime
from collections import Counter
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ── Config ────────────────────────────────────────────────────────────────────
APIFY_TOKEN   = os.getenv('APIFY_API_TOKEN', '')
FB_TOKEN      = os.getenv('FACEBOOK_ACCESS_TOKEN', '')
YT_KEY        = os.getenv('YOUTUBE_API_KEY', '')
DATABASE_URL  = os.getenv('DIRECT_URL') or os.getenv('DATABASE_URL','').replace('?pgbouncer=true','')
FB_BASE       = "https://graph.facebook.com/v19.0"
YT_BASE       = "https://www.googleapis.com/youtube/v3"
APIFY_ACTOR   = "clockworks~free-tiktok-scraper"
DELAY         = 1.0

# ── Colors ────────────────────────────────────────────────────────────────────
G="\033[92m"; Y="\033[93m"; R="\033[91m"; B="\033[94m"; W="\033[97m"; D="\033[2m"; NC="\033[0m"

def ok(m):   print(f"  {G}✓{NC} {m}")
def err(m):  print(f"  {R}✗{NC} {m}")
def info(m): print(f"  {Y}→{NC} {m}")
def fmt(n):
    n = int(n or 0)
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

# ── DB helpers ────────────────────────────────────────────────────────────────
def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def db_query(sql, params=None):
    conn = db_conn(); cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = [dict(r) for r in cur.fetchall()]
    conn.close(); return rows

def upsert_posts(posts: list) -> int:
    """Lưu danh sách post vào social_video_report."""
    if not posts: return 0
    conn = db_conn(); cur = conn.cursor(); saved = 0
    try:
        for p in posts:
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
        conn.commit(); return saved
    except Exception as e:
        conn.rollback(); raise e
    finally:
        conn.close()

# ── Helpers ───────────────────────────────────────────────────────────────────
def month_range(year, month):
    last = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last}"

def calc_max_videos(year, month):
    today = date.today()
    days  = today.day if (year == today.year and month == today.month) \
            else calendar.monthrange(year, month)[1]
    return min(days * 5 + 10, 100)

def extract_hashtags(text):
    return re.findall(r'#\w+', text or '')

def get_channels(platform_like, team=None, single=None):
    if single:
        return [{'name': single, 'channel_id': single, 'link_channel': '',
                 'team': '', 'owner': '', 'platform': platform_like}]
    params = [f'%{platform_like}%']
    extra  = ""
    if team:
        extra = " AND LOWER(team_traffic) LIKE LOWER(%s)"
        params.append(f"%{team}%")
    return db_query(f"""
        SELECT name, channel_id, link_channel,
               team_traffic AS team, owner, platform
        FROM huyk_channels
        WHERE LOWER(platform) LIKE %s
          AND status IN ('Đang hoạt động', 'ON')
        {extra}
        ORDER BY team_traffic, name
    """, params)

def extract_uid(channel_id, link, platform=''):
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

# ── TikTok (Apify) ────────────────────────────────────────────────────────────
def crawl_tiktok(channels, year, month, date_from, date_to, max_videos):
    results = []
    for ch in channels:
        uid = extract_uid(ch.get('channel_id',''), ch.get('link_channel',''))
        if not uid: continue
        try:
            r = requests.post(
                f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
                params={"token": APIFY_TOKEN, "timeout": 120},
                json={
                    "profiles":        [uid],
                    "resultsPerPage":  max_videos,
                    # Dừng khi gặp video cũ hơn date_from → không tốn credits tháng trước
                    "oldestVideoDate": date_from,
                },
                timeout=150,
            )
            if not r.ok:
                err(f"TikTok {uid}: {r.status_code}"); continue
            items = r.json() or []
            posts, old_streak = [], 0
            for item in items:
                iso = (item.get('createTimeISO') or '')[:10]
                ts  = item.get('createTime', 0)
                if not iso and ts:
                    iso = datetime.utcfromtimestamp(int(ts)).strftime('%Y-%m-%d')
                if not iso: continue
                if iso < date_from:
                    old_streak += 1
                    if old_streak >= 3: break  # smart-stop
                    continue
                old_streak = 0
                if iso > date_to: continue
                posts.append({
                    'platform': 'tiktok', 'post_id': item.get('id',''),
                    'channel_name': ch['name'], 'username': uid,
                    'owner': ch.get('owner',''), 'team': ch.get('team',''),
                    'title': (item.get('text') or '')[:500],
                    'hashtags': extract_hashtags(item.get('text','')),
                    'views': int(item.get('playCount',0)),
                    'likes': int(item.get('diggCount',0)),
                    'comments': int(item.get('commentCount',0)),
                    'shares': int(item.get('shareCount',0)),
                    'followers': int(item.get('authorMeta',{}).get('fans',0)),
                    'url': item.get('webVideoUrl',''),
                    'published_at': iso, 'year': year, 'month': month,
                    'source': 'apify',
                })
            n = upsert_posts(posts)
            results.append({'ch': ch['name'], 'uid': uid, 'posts': len(posts),
                            'views': sum(p['views'] for p in posts), 'saved': n,
                            'skipped': len(items)-len(posts)})
            print(f"    {G}✓{NC} @{uid:<30} {len(posts):>3} videos | {G}{fmt(sum(p['views'] for p in posts))}{NC} views | {D}[skip {len(items)-len(posts)}]{NC}")
        except Exception as e:
            err(f"TikTok {uid}: {e}")
        time.sleep(DELAY)
    return results

# ── YouTube (Data API) ────────────────────────────────────────────────────────
def _yt_resolve_channel(uid):
    uid = uid.strip().strip('\n')
    if uid.startswith('UC'): return uid
    handle = uid.lstrip('@')
    r = requests.get(f"{YT_BASE}/channels", params={
        "key": YT_KEY, "forHandle": f"@{handle}", "part": "id,statistics"}, timeout=10)
    items = r.json().get("items",[]) if r.ok else []
    if items: return items[0]['id']
    r2 = requests.get(f"{YT_BASE}/channels", params={
        "key": YT_KEY, "forUsername": handle, "part": "id,statistics"}, timeout=10)
    items2 = r2.json().get("items",[]) if r2.ok else []
    return items2[0]['id'] if items2 else None

def crawl_youtube(channels, year, month, date_from, date_to):
    results = []
    for ch in channels:
        uid = extract_uid(ch.get('channel_id',''), ch.get('link_channel',''), 'youtube')
        if not uid: continue
        try:
            channel_id = _yt_resolve_channel(uid)
            if not channel_id:
                err(f"YT: không tìm thấy channel {uid}"); continue

            # Lấy followers
            ch_r = requests.get(f"{YT_BASE}/channels", params={
                "key": YT_KEY, "id": channel_id, "part": "statistics"}, timeout=10)
            followers = int(ch_r.json().get('items',[{}])[0].get('statistics',{}).get('subscriberCount',0)) if ch_r.ok else 0

            # Search videos trong tháng
            sr = requests.get(f"{YT_BASE}/search", params={
                "key": YT_KEY, "channelId": channel_id, "part": "id",
                "type": "video", "order": "date", "maxResults": 50,
                "publishedAfter": f"{date_from}T00:00:00Z",
                "publishedBefore": f"{date_to}T23:59:59Z",
            }, timeout=15)
            if not sr.ok: continue
            video_ids = [i['id']['videoId'] for i in sr.json().get('items',[])]
            if not video_ids:
                print(f"    {D}@{uid:<30} 0 videos{NC}"); continue

            vr = requests.get(f"{YT_BASE}/videos", params={
                "key": YT_KEY, "id": ",".join(video_ids), "part": "snippet,statistics"
            }, timeout=15)
            posts = []
            for item in (vr.json().get('items',[]) if vr.ok else []):
                s   = item.get('statistics',{})
                pub = item['snippet']['publishedAt'][:10]
                tags = item['snippet'].get('tags',[])[:10]
                posts.append({
                    'platform': 'youtube', 'post_id': item['id'],
                    'channel_name': ch['name'], 'username': uid,
                    'owner': ch.get('owner',''), 'team': ch.get('team',''),
                    'title': item['snippet']['title'][:500],
                    'hashtags': [f"#{t}" for t in tags],
                    'views': int(s.get('viewCount',0)),
                    'likes': int(s.get('likeCount',0)),
                    'comments': int(s.get('commentCount',0)),
                    'shares': 0, 'followers': followers,
                    'url': f"https://youtube.com/watch?v={item['id']}",
                    'published_at': pub, 'year': year, 'month': month,
                    'source': 'api',
                })
            n = upsert_posts(posts)
            results.append({'ch': ch['name'], 'uid': uid, 'posts': len(posts),
                            'views': sum(p['views'] for p in posts), 'saved': n})
            print(f"    {G}✓{NC} @{uid:<30} {len(posts):>3} videos | {G}{fmt(sum(p['views'] for p in posts))}{NC} views")
        except Exception as e:
            err(f"YouTube {uid}: {e}")
        time.sleep(0.5)
    return results

# ── Facebook (Graph API nếu admin, Apify nếu không) ──────────────────────────
_fb_admin_pages = None

def _get_admin_pages():
    global _fb_admin_pages
    if _fb_admin_pages is not None: return _fb_admin_pages
    _fb_admin_pages = {}
    try:
        url = f"{FB_BASE}/me/accounts"
        while url:
            r = requests.get(url, params={
                "access_token": FB_TOKEN,
                "fields": "id,name,access_token,followers_count", "limit": 50
            }, timeout=15)
            if not r.ok: break
            data = r.json()
            for p in data.get("data",[]):
                if p.get("access_token"):
                    _fb_admin_pages[p['id']] = p
                    _fb_admin_pages[p.get('name','').lower()] = p
            url = data.get("paging",{}).get("next")
    except: pass
    return _fb_admin_pages

def _fb_get_page_token(uid):
    pages = _get_admin_pages()
    # Thử theo ID hoặc username
    r = requests.get(f"{FB_BASE}/{uid}", params={
        "access_token": FB_TOKEN, "fields": "id,name,access_token,followers_count"
    }, timeout=10)
    if r.ok:
        d = r.json()
        if d.get("access_token"):
            return d["id"], d["access_token"], int(d.get("followers_count",0))
    return uid, None, 0

def crawl_facebook_api(page_id, page_token, followers, ch, year, month, date_from, date_to):
    since_ts = int(datetime.strptime(date_from, "%Y-%m-%d").timestamp())
    until_ts = int(datetime.strptime(date_to, "%Y-%m-%d").timestamp()) + 86399
    posts_r = requests.get(f"{FB_BASE}/{page_id}/posts", params={
        "access_token": page_token,
        "fields": "id,message,created_time,likes.summary(true),comments.summary(true),shares",
        "since": since_ts, "until": until_ts, "limit": 50,
    }, timeout=15)
    if not posts_r.ok: return []
    posts = []
    for post in posts_r.json().get("data",[]):
        likes    = post.get("likes",{}).get("summary",{}).get("total_count",0)
        comments = post.get("comments",{}).get("summary",{}).get("total_count",0)
        shares   = post.get("shares",{}).get("count",0)
        msg      = post.get("message","") or ""
        pub      = post.get("created_time","")[:10]
        posts.append({
            'platform': 'facebook', 'post_id': post['id'],
            'channel_name': ch['name'], 'username': ch.get('channel_id',''),
            'owner': ch.get('owner',''), 'team': ch.get('team',''),
            'title': msg[:500], 'hashtags': extract_hashtags(msg),
            'views': 0, 'likes': likes, 'comments': comments, 'shares': shares,
            'followers': followers, 'url': f"https://facebook.com/{post['id']}",
            'published_at': pub, 'year': year, 'month': month, 'source': 'api',
        })
    return posts

def crawl_facebook_apify(uid, ch, year, month, date_from, date_to, max_videos):
    if not APIFY_TOKEN: return []
    r = requests.post(
        "https://api.apify.com/v2/acts/apify~facebook-posts-scraper/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN, "timeout": 120},
        json={"startUrls": [{"url": f"https://www.facebook.com/{uid}"}],
              "resultsLimit": max_videos},
        timeout=150,
    )
    if not r.ok: return []
    posts = []
    for item in (r.json() or []):
        pub = (item.get('date') or '')[:10]
        if not pub or not (date_from <= pub <= date_to): continue
        msg = item.get('text','') or ''
        posts.append({
            'platform': 'facebook', 'post_id': item.get('postId', item.get('url','')),
            'channel_name': ch['name'], 'username': uid,
            'owner': ch.get('owner',''), 'team': ch.get('team',''),
            'title': msg[:500], 'hashtags': extract_hashtags(msg),
            'views': int(item.get('videoViewCount',0)),
            'likes': int(item.get('likes',0)),
            'comments': int(item.get('comments',0)),
            'shares': int(item.get('shares',0)),
            'followers': 0, 'url': item.get('url',''),
            'published_at': pub, 'year': year, 'month': month, 'source': 'apify',
        })
    return posts

def crawl_facebook(channels, year, month, date_from, date_to, max_videos):
    _get_admin_pages()  # preload
    results = []
    for ch in channels:
        uid = extract_uid(ch.get('channel_id',''), ch.get('link_channel',''), 'facebook')
        if not uid: continue
        try:
            page_id, page_token, followers = _fb_get_page_token(uid)
            if page_token:
                posts  = crawl_facebook_api(page_id, page_token, followers, ch, year, month, date_from, date_to)
                source = f"{G}[API]{NC}"
            else:
                posts  = crawl_facebook_apify(uid, ch, year, month, date_from, date_to, max_videos)
                source = f"{Y}[Apify]{NC}"
            n = upsert_posts(posts)
            results.append({'ch': ch['name'], 'uid': uid, 'posts': len(posts),
                            'likes': sum(p['likes'] for p in posts), 'saved': n})
            print(f"    {source} @{uid:<28} {len(posts):>3} posts | {G}{fmt(sum(p['likes'] for p in posts))}{NC} likes")
        except Exception as e:
            err(f"Facebook {uid}: {e}")
        time.sleep(DELAY)
    return results

# ── Instagram (Graph API nếu Business, Apify nếu không) ──────────────────────
_ig_accounts = None

def _get_ig_business_accounts():
    global _ig_accounts
    if _ig_accounts is not None: return _ig_accounts
    _ig_accounts = {}
    try:
        url = f"{FB_BASE}/me/accounts"
        while url:
            r = requests.get(url, params={
                "access_token": FB_TOKEN,
                "fields": "id,name,instagram_business_account{id,username,followers_count}",
                "limit": 50,
            }, timeout=15)
            if not r.ok: break
            data = r.json()
            for p in data.get("data",[]):
                ig = p.get("instagram_business_account")
                if ig:
                    _ig_accounts[ig.get("username","").lower()] = {
                        "ig_id": ig["id"], "username": ig.get("username",""),
                        "followers": ig.get("followers_count",0)
                    }
            url = data.get("paging",{}).get("next")
    except: pass
    return _ig_accounts

def crawl_instagram_api(ig_info, ch, year, month, date_from, date_to):
    since_ts = int(datetime.strptime(date_from, "%Y-%m-%d").timestamp())
    until_ts = int(datetime.strptime(date_to, "%Y-%m-%d").timestamp()) + 86399
    r = requests.get(f"{FB_BASE}/{ig_info['ig_id']}/media", params={
        "access_token": FB_TOKEN,
        "fields": "id,caption,media_type,timestamp,like_count,comments_count",
        "since": since_ts, "until": until_ts, "limit": 50,
    }, timeout=15)
    if not r.ok: return []
    posts = []
    for item in r.json().get("data",[]):
        cap = item.get("caption","") or ""
        pub = item.get("timestamp","")[:10]
        posts.append({
            'platform': 'instagram', 'post_id': item['id'],
            'channel_name': ch['name'], 'username': ig_info['username'],
            'owner': ch.get('owner',''), 'team': ch.get('team',''),
            'title': cap[:500], 'hashtags': extract_hashtags(cap),
            'views': 0, 'likes': int(item.get('like_count',0)),
            'comments': int(item.get('comments_count',0)), 'shares': 0,
            'followers': ig_info['followers'],
            'url': f"https://instagram.com/p/{item['id']}",
            'published_at': pub, 'year': year, 'month': month, 'source': 'api',
        })
    return posts

def crawl_instagram_apify(uid, ch, year, month, date_from, date_to, max_videos):
    if not APIFY_TOKEN: return []
    r = requests.post(
        "https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN, "timeout": 120},
        json={"directUrls": [f"https://www.instagram.com/{uid}/"],
              "resultsType": "posts", "resultsLimit": max_videos},
        timeout=150,
    )
    if not r.ok: return []
    posts = []
    for item in (r.json() or []):
        pub = (item.get('timestamp') or '')[:10]
        if not pub or not (date_from <= pub <= date_to): continue
        cap = item.get('caption','') or ''
        posts.append({
            'platform': 'instagram', 'post_id': item.get('id',''),
            'channel_name': ch['name'], 'username': uid,
            'owner': ch.get('owner',''), 'team': ch.get('team',''),
            'title': cap[:500], 'hashtags': extract_hashtags(cap),
            'views': int(item.get('videoViewCount',0)),
            'likes': int(item.get('likesCount',0)),
            'comments': int(item.get('commentsCount',0)), 'shares': 0,
            'followers': int(item.get('followersCount',0)),
            'url': item.get('url',''),
            'published_at': pub, 'year': year, 'month': month, 'source': 'apify',
        })
    return posts

def crawl_instagram(channels, year, month, date_from, date_to, max_videos):
    ig_biz = _get_ig_business_accounts()
    results = []
    for ch in channels:
        uid = extract_uid(ch.get('channel_id',''), ch.get('link_channel',''), 'instagram')
        uid = uid.lstrip('@').strip()
        if not uid: continue
        try:
            ig_info = ig_biz.get(uid.lower())
            if ig_info:
                posts  = crawl_instagram_api(ig_info, ch, year, month, date_from, date_to)
                source = f"{G}[API]{NC}"
            else:
                posts  = crawl_instagram_apify(uid, ch, year, month, date_from, date_to, max_videos)
                source = f"{Y}[Apify]{NC}"
            n = upsert_posts(posts)
            results.append({'ch': ch['name'], 'uid': uid, 'posts': len(posts),
                            'likes': sum(p['likes'] for p in posts), 'saved': n})
            print(f"    {source} @{uid:<28} {len(posts):>3} posts | {G}{fmt(sum(p['likes'] for p in posts))}{NC} likes")
        except Exception as e:
            err(f"Instagram {uid}: {e}")
        time.sleep(DELAY)
    return results

# ── Main ──────────────────────────────────────────────────────────────────────
def run(year, month, platforms=None, team=None, single_channel=None, single_platform=None):
    today      = date.today()
    date_from  = f"{year}-{month:02d}-01"
    date_to    = today.strftime("%Y-%m-%d") if (year==today.year and month==today.month) \
                 else f"{year}-{month:02d}-{calendar.monthrange(year,month)[1]}"
    max_videos = min((today.day if (year==today.year and month==today.month)
                      else calendar.monthrange(year,month)[1]) * 5 + 10, 100)

    all_platforms = platforms or ['tiktok','youtube','facebook','instagram']

    print(f"\n{B}{'═'*62}{NC}")
    print(f"{W}  Social Insights — Tháng {month}/{year}  ({date_from} → {date_to}){NC}")
    print(f"  Platforms : {', '.join(all_platforms)}")
    if team: print(f"  Team      : {Y}{team}{NC}")
    print(f"  Max fetch : {max_videos} items/kênh")
    print(f"{B}{'═'*62}{NC}\n")

    grand_results = {}

    for plat in all_platforms:
        print(f"\n{W}  ── {plat.upper()} ──────────────────────────────────────{NC}")
        channels = get_channels(plat, team=team, single=single_channel if single_platform==plat else None)
        if not channels:
            info(f"Không có kênh {plat} nào"); continue
        print(f"  {len(channels)} kênh\n")

        if plat == 'tiktok':
            if not APIFY_TOKEN or 'placeholder' in APIFY_TOKEN:
                err("Thiếu APIFY_API_TOKEN"); continue
            res = crawl_tiktok(channels, year, month, date_from, date_to, max_videos)
        elif plat == 'youtube':
            if not YT_KEY:
                err("Thiếu YOUTUBE_API_KEY"); continue
            res = crawl_youtube(channels, year, month, date_from, date_to)
        elif plat == 'facebook':
            if not FB_TOKEN:
                err("Thiếu FACEBOOK_ACCESS_TOKEN"); continue
            res = crawl_facebook(channels, year, month, date_from, date_to, max_videos)
        elif plat == 'instagram':
            if not FB_TOKEN:
                err("Thiếu FACEBOOK_ACCESS_TOKEN"); continue
            res = crawl_instagram(channels, year, month, date_from, date_to, max_videos)
        else:
            continue

        grand_results[plat] = res
        total_posts = sum(r.get('posts',0) for r in res)
        total_saved = sum(r.get('saved',0) for r in res)
        ok(f"{plat}: {len(res)} kênh OK | {total_posts} posts → {total_saved} lưu DB")

    # ── Tổng kết ─────────────────────────────────────────────────────────────
    print(f"\n{B}{'═'*62}{NC}")
    print(f"{W}  TỔNG KẾT — Tháng {month}/{year}{NC}")
    print(f"{B}{'═'*62}{NC}")
    print(f"  {'Platform':<12} {'Kênh':>6} {'Posts':>7} {'Saved':>7}")
    print(f"  {'─'*12} {'─'*6} {'─'*7} {'─'*7}")
    for plat, res in grand_results.items():
        print(f"  {plat:<12} {len(res):>6} {sum(r.get('posts',0) for r in res):>7} "
              f"{G}{sum(r.get('saved',0) for r in res):>7}{NC}")
    print(f"\n  {G}Bảng DB: social_video_report{NC}\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--month',    type=int, default=date.today().month)
    parser.add_argument('--year',     type=int, default=date.today().year)
    parser.add_argument('--platform', type=str, default=None,
                        help='tiktok | youtube | facebook | instagram (mặc định: tất cả)')
    parser.add_argument('--team',     type=str, default=None)
    parser.add_argument('--channel',  type=str, default=None, help='Test 1 kênh cụ thể')
    args = parser.parse_args()

    platforms = [args.platform] if args.platform else None
    run(year=args.year, month=args.month,
        platforms=platforms, team=args.team,
        single_channel=args.channel, single_platform=args.platform)
