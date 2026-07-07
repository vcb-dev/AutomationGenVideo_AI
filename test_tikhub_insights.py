"""
Test fetch TikTok insights qua TikHub API.
- Filter đúng ngày phía server → không tốn tiền cho video tháng khác
- Free: 10.000 request/tháng

Đăng ký key: https://tikhub.io → Dashboard → API Keys

Chạy:
  python test_tikhub_insights.py --month 5
  python test_tikhub_insights.py --month 5 --channel huyk.xuongkimhoan2
  python test_tikhub_insights.py --month 5 --team "Team K1"
"""

import os, sys, re, json, time, argparse, calendar, requests, psycopg2, psycopg2.extras
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ── Config ────────────────────────────────────────────────────────────────────
TIKHUB_TOKEN = os.getenv('TIKHUB_API_KEY', '')
DATABASE_URL  = os.getenv('DIRECT_URL') or os.getenv('DATABASE_URL','').replace('?pgbouncer=true','')
TIKHUB_BASE   = "https://api.tikhub.io"
DELAY         = 0.5  # Giây nghỉ giữa các kênh (TikHub nhanh hơn Apify)

# ── Colors ────────────────────────────────────────────────────────────────────
G="\033[92m"; Y="\033[93m"; R="\033[91m"; B="\033[94m"; W="\033[97m"; D="\033[2m"; NC="\033[0m"

def ok(m):   print(f"  {G}✓{NC} {m}")
def err(m):  print(f"  {R}✗{NC} {m}")
def fmt(n):
    n = int(n or 0)
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

# ── DB ────────────────────────────────────────────────────────────────────────
def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def db_query(sql, params=None):
    conn = db_conn(); cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = [dict(r) for r in cur.fetchall()]
    conn.close(); return rows

def upsert_posts(posts: list) -> int:
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
                   %s, %s, %s::date, %s, %s, 'tikhub', NOW())
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
                p['year'], p['month']
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

def extract_hashtags(text):
    return re.findall(r'#\w+', text or '')

def get_channels(team=None, single=None):
    if single:
        rows = db_query("""
            SELECT name, channel_id, link_channel, team_traffic AS team, owner
            FROM huyk_channels
            WHERE LOWER(channel_id) = LOWER(%s)
               OR LOWER(link_channel) LIKE LOWER(%s)
            LIMIT 1
        """, [single, f"%{single}%"])
        return rows or [{'name': single, 'channel_id': single,
                         'link_channel': '', 'team': '', 'owner': ''}]
    params = ['%tiktok%']
    extra  = ""
    if team:
        extra = " AND LOWER(team_traffic) LIKE LOWER(%s)"
        params.append(f"%{team}%")
    return db_query(f"""
        SELECT name, channel_id, link_channel,
               team_traffic AS team, owner
        FROM huyk_channels
        WHERE LOWER(platform) LIKE %s
          AND status IN ('Đang hoạt động', 'ON')
        {extra}
        ORDER BY team_traffic, name
    """, params)

def extract_username(channel_id, link):
    uid = (channel_id or '').strip().strip('\n').lstrip('@')
    if uid: return uid
    if 'tiktok.com/@' in (link or ''):
        return link.split('tiktok.com/@')[-1].split('?')[0].strip('/')
    return ''

# ── TikHub API ────────────────────────────────────────────────────────────────
_sec_uid_cache = {}

def tikhub_get_sec_uid(username: str) -> str:
    """Lấy secUid từ username — cache lại để tránh gọi lặp."""
    if username in _sec_uid_cache:
        return _sec_uid_cache[username]

    r = requests.get(
        f"{TIKHUB_BASE}/api/v1/tiktok/web/fetch_user_profile",
        headers={"Authorization": f"Bearer {TIKHUB_TOKEN}"},
        params={"uniqueId": username},
        timeout=15,
    )
    if not r.ok:
        raise Exception(f"TikHub profile error {r.status_code}: {r.text[:150]}")

    data    = r.json()
    # Cấu trúc: data.data.userInfo.user.secUid
    sec_uid = (data.get("data",{}).get("userInfo",{}).get("user",{}).get("secUid") or
               data.get("data",{}).get("secUid") or
               data.get("secUid") or "")

    if not sec_uid:
        raise Exception(f"Không lấy được secUid cho @{username}. Response: {str(data)[:150]}")

    _sec_uid_cache[username] = sec_uid
    return sec_uid


def tikhub_get_user_videos(username: str, date_from: str, date_to: str) -> list:
    """
    Lấy video của 1 user trong khoảng ngày qua TikHub.
    Bước 1: lấy secUid từ username
    Bước 2: fetch video với secUid + smart-stop theo ngày
    """
    since_ts = int(datetime.strptime(date_from, "%Y-%m-%d").timestamp())
    until_ts = int(datetime.strptime(date_to,   "%Y-%m-%d").timestamp()) + 86399

    sec_uid  = tikhub_get_sec_uid(username)
    headers  = {"Authorization": f"Bearer {TIKHUB_TOKEN}"}
    all_videos = []
    cursor = 0

    while True:
        params = {"secUid": sec_uid, "count": 20}
        if cursor: params["cursor"] = cursor

        r = requests.get(
            f"{TIKHUB_BASE}/api/v1/tiktok/web/fetch_user_post",
            headers=headers, params=params, timeout=30,
        )
        if not r.ok:
            raise Exception(f"TikHub error {r.status_code}: {r.text[:200]}")

        data     = r.json()
        inner    = data.get("data") or data
        items    = inner.get("aweme_list") or inner.get("videos") or \
                   inner.get("items") or []
        has_more = bool(inner.get("has_more", False))
        next_cur = inner.get("cursor") or inner.get("max_cursor") or 0

        if not items: break

        stop = False
        for item in items:
            ts = int(item.get("create_time") or 0)
            if ts and ts < since_ts:
                stop = True; break   # Video cũ → dừng
            if ts and ts > until_ts:
                continue             # Video mới hơn date_to → bỏ
            all_videos.append(item)

        if stop or not has_more or not next_cur: break
        cursor = next_cur

    return all_videos


def parse_tikhub_video(item: dict, ch: dict, year: int, month: int) -> dict:
    """Parse video từ TikHub response."""
    # TikHub có thể trả nhiều format khác nhau
    vid_id = item.get("aweme_id") or item.get("video_id") or item.get("id") or ""
    title  = item.get("desc") or item.get("title") or item.get("text") or ""
    url    = (item.get("video") or {}).get("play_addr", {}).get("url_list", [""])[0] or \
             item.get("share_url") or item.get("url") or \
             f"https://www.tiktok.com/@{ch.get('channel_id','')}/video/{vid_id}"

    # Stats — TikHub dùng field "statistics"
    stats    = item.get("statistics") or {}
    views    = int(stats.get("play_count",0)    or stats.get("playCount",0)    or 0)
    likes    = int(stats.get("digg_count",0)    or stats.get("diggCount",0)    or 0)
    cmts     = int(stats.get("comment_count",0) or stats.get("commentCount",0) or 0)
    shares   = int(stats.get("share_count",0)   or stats.get("shareCount",0)   or 0)

    # Followers — từ author_user_info hoặc author
    author    = item.get("author_user_info") or item.get("author") or {}
    followers = int(author.get("follower_count",0) or author.get("fans",0) or 0)

    # Date
    create_ts = item.get("create_time") or 0
    pub_date  = datetime.utcfromtimestamp(int(create_ts)).strftime("%Y-%m-%d") \
                if create_ts else ""

    return {
        "platform":    "tiktok",
        "post_id":     vid_id,
        "channel_name": ch.get("name", ""),
        "username":    ch.get("channel_id", ""),
        "owner":       ch.get("owner", ""),
        "team":        ch.get("team", ""),
        "title":       title[:500],
        "hashtags":    extract_hashtags(title),
        "views":       views,
        "likes":       likes,
        "comments":    cmts,
        "shares":      shares,
        "followers":   followers,
        "url":         url,
        "published_at": pub_date,
        "year":        year,
        "month":       month,
    }

# ── Main ──────────────────────────────────────────────────────────────────────
def run(year, month, team=None, single=None):
    today     = date.today()
    date_from = f"{year}-{month:02d}-01"
    date_to   = today.strftime("%Y-%m-%d") if (year == today.year and month == today.month) \
                else f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"

    print(f"\n{B}{'═'*62}{NC}")
    print(f"{W}  TikHub Insights — Tháng {month}/{year}  ({date_from} → {date_to}){NC}")
    if team:   print(f"  Filter team : {Y}{team}{NC}")
    if single: print(f"  Test kênh   : {Y}@{single}{NC}")
    print(f"  {G}Filter ngày phía server{NC} — chỉ nhận video trong tháng")
    print(f"{B}{'═'*62}{NC}\n")

    if not TIKHUB_TOKEN or TIKHUB_TOKEN == 'your_tikhub_key_here':
        err("Chưa có TIKHUB_API_KEY trong .env")
        err("Đăng ký free tại: https://tikhub.io → Dashboard → API Keys")
        sys.exit(1)

    channels = get_channels(team=team, single=single)
    if not channels:
        err("Không tìm thấy kênh nào"); sys.exit(1)

    print(f"{G}✓{NC} Tổng {W}{len(channels)}{NC} kênh TikTok\n")
    print(f"  {'#':>3}  {'Kênh':<28} {'Team':<14} {'Videos':>7} {'Views':>8} {'Likes':>7} {'Cmt':>6} {'Shares':>7} {'Followers':>10}")
    print(f"  {'─'*3}  {'─'*28} {'─'*14} {'─'*7} {'─'*8} {'─'*7} {'─'*6} {'─'*7} {'─'*10}")

    all_posts     = []
    channel_stats = []
    errors        = []

    for idx, ch in enumerate(channels, 1):
        uid  = extract_username(ch.get('channel_id',''), ch.get('link_channel',''))
        name = ch.get('name', uid)[:26]
        team_ch = (ch.get('team') or '')[:12]

        if not uid:
            print(f"  {idx:>3}. {D}{name:<28} SKIP — không có username{NC}")
            continue

        try:
            items = tikhub_get_user_videos(uid, date_from, date_to)
            posts = [parse_tikhub_video(item, {**ch, 'channel_id': uid}, year, month)
                     for item in items if item]
            posts = [p for p in posts if p.get('post_id')]

            n_saved = upsert_posts(posts)
            views    = sum(p['views']    for p in posts)
            likes    = sum(p['likes']    for p in posts)
            comments = sum(p['comments'] for p in posts)
            shares   = sum(p['shares']   for p in posts)
            followers = posts[0]['followers'] if posts else 0

            print(f"  {idx:>3}. {W}{name:<28}{NC} {D}{team_ch:<14}{NC} "
                  f"{len(posts):>7} {G}{fmt(views):>8}{NC} "
                  f"{fmt(likes):>7} {fmt(comments):>6} {fmt(shares):>7} "
                  f"{fmt(followers):>10}  {G}[DB {n_saved}✓]{NC}")

            channel_stats.append({
                'channel': ch.get('name', uid), 'username': uid,
                'owner': ch.get('owner',''), 'team': ch.get('team',''),
                'video_count': len(posts), 'followers': followers,
                'total_views': views, 'total_likes': likes,
                'total_comments': comments, 'total_shares': shares,
            })
            all_posts.extend(posts)

        except Exception as e:
            err(f"{name}: {e}")
            errors.append({'channel': name, 'error': str(e)})

        time.sleep(DELAY)

    # ── Tổng kết ─────────────────────────────────────────────────────────────
    gv = sum(c['total_views']    for c in channel_stats)
    gl = sum(c['total_likes']    for c in channel_stats)
    gc = sum(c['total_comments'] for c in channel_stats)
    gs = sum(c['total_shares']   for c in channel_stats)
    gp = sum(c['video_count']    for c in channel_stats)

    print(f"\n{B}{'─'*62}{NC}")
    print(f"{W}  TỔNG KẾT — Tháng {month}/{year}{NC}")
    print(f"{B}{'─'*62}{NC}")
    print(f"  Kênh OK     : {G}{len(channel_stats)}/{len(channels)}{NC}")
    print(f"  Kênh lỗi    : {R}{len(errors)}{NC}")
    print(f"  Tổng video  : {W}{gp}{NC}")
    print(f"  Tổng views  : {G}{fmt(gv)}{NC}")
    print(f"  Tổng likes  : {fmt(gl)}")
    print(f"  Tổng cmts   : {fmt(gc)}")
    print(f"  Lưu DB      : {G}social_video_report{NC}")
    print(f"  Nguồn       : {G}TikHub (filter đúng ngày){NC}")

    # Top 10
    top = sorted(all_posts, key=lambda x: x['views'], reverse=True)[:10]
    if top:
        print(f"\n{W}  TOP 10 VIEWS:{NC}")
        for i, p in enumerate(top, 1):
            print(f"  {i:>2}. {G}{fmt(p['views']):>8}{NC} | {p['channel_name'][:22]:<22} | {p['title'][:40]}")

    # Ước tính request đã dùng
    reqs_used = len(channel_stats) * 2  # ~2 requests/kênh trung bình
    print(f"\n{D}  TikHub requests ước tính: ~{reqs_used} / 10.000 free/tháng{NC}")
    print(f"{B}{'═'*62}{NC}\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--month',   type=int, default=date.today().month)
    parser.add_argument('--year',    type=int, default=date.today().year)
    parser.add_argument('--team',    type=str, default=None)
    parser.add_argument('--channel', type=str, default=None)
    args = parser.parse_args()
    run(year=args.year, month=args.month, team=args.team, single=args.channel)
