"""
Test fetch TikTok insights cho tất cả kênh trong huyk_channels.
- Chỉ lấy video trong tháng cần → tối ưu chi phí Apify
- Smart-stop: gặp video cũ hơn tháng cần → dừng ngay

Chạy:
  python test_tiktok_insights.py                    # tháng hiện tại
  python test_tiktok_insights.py --month 5 --year 2026
  python test_tiktok_insights.py --month 5 --team "Team K1"
  python test_tiktok_insights.py --channel huyk.xuongkimhoan2  # test 1 kênh
"""

import os, sys, json, time, argparse, calendar, requests, psycopg2, psycopg2.extras
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ── Config ────────────────────────────────────────────────────────────────────
APIFY_TOKEN  = os.getenv('APIFY_API_TOKEN', '')
DATABASE_URL = os.getenv('DIRECT_URL') or os.getenv('DATABASE_URL','').replace('?pgbouncer=true','')
ACTOR        = "clockworks~free-tiktok-scraper"
DELAY        = 1.0  # Giây nghỉ giữa các kênh

def calc_max_videos(year: int, month: int) -> int:
    """Tính số video tối ưu cần fetch: số ngày trong khoảng × 5 video/ngày + buffer."""
    today = date.today()
    if year == today.year and month == today.month:
        days = today.day
    else:
        days = calendar.monthrange(year, month)[1]
    return min(days * 5 + 10, 100)  # Tối đa 100 để tránh tốn credits

# ── Terminal colors ───────────────────────────────────────────────────────────
G="\033[92m"; Y="\033[93m"; R="\033[91m"; B="\033[94m"; W="\033[97m"; D="\033[2m"; NC="\033[0m"

def ok(m):   print(f"  {G}✓{NC} {m}")
def err(m):  print(f"  {R}✗{NC} {m}")
def info(m): print(f"  {Y}→{NC} {m}")
def dim(m):  print(f"  {D}{m}{NC}")
def fmt(n):
    n = int(n or 0)
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

# ── DB ────────────────────────────────────────────────────────────────────────
def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def db_query(sql, params=None):
    conn = db_conn()
    cur  = conn.cursor()
    cur.execute(sql, params or ())
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def extract_hashtags(title: str) -> list:
    import re
    return re.findall(r'#\w+', title or '')


def save_to_db(videos: list, channel_name: str, username: str,
               team: str, owner: str, year: int, month: int,
               followers: int) -> int:
    """Lưu từng video vào bảng tiktok_video_report."""
    if not videos:
        return 0

    conn = db_conn()
    cur  = conn.cursor()
    saved = 0
    try:
        for v in videos:
            pub = v.get('date') or None
            tags = extract_hashtags(v.get('title', ''))
            try:
                cur.execute("""
                    INSERT INTO tiktok_video_report
                      (id, video_id, channel_name, username, owner, team,
                       title, hashtags, views, likes, comments, shares,
                       followers, video_url, published_at,
                       year, month, synced_at)
                    VALUES
                      (gen_random_uuid(), %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s,
                       %s, %s, %s::date,
                       %s, %s, NOW())
                    ON CONFLICT (video_id) DO UPDATE SET
                      views     = EXCLUDED.views,
                      likes     = EXCLUDED.likes,
                      comments  = EXCLUDED.comments,
                      shares    = EXCLUDED.shares,
                      followers = EXCLUDED.followers,
                      hashtags  = EXCLUDED.hashtags,
                      synced_at = NOW()
                """, (
                    v.get('video_id', ''), channel_name, username, owner, team,
                    v.get('title', '')[:500], tags,
                    v['views'], v['likes'], v['comments'], v['shares'],
                    followers, v.get('url', ''), pub,
                    year, month
                ))
                saved += 1
            except Exception as e:
                print(f"    {D}video skip: {e}{NC}")
        conn.commit()
        return saved
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_channels(team=None, single=None):
    if single:
        return [{'name': single, 'channel_id': single, 'link_channel': '', 'team': '', 'owner': ''}]
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

# ── Apify ─────────────────────────────────────────────────────────────────────
def apify_fetch(username: str, max_videos: int, date_from: str, date_to: str) -> tuple[list, dict]:
    """
    Fetch TikTok videos qua Apify.
    Smart-stop: dừng khi gặp video cũ hơn date_from → tiết kiệm credits.
    Trả về (videos_in_range, meta{fetched, skipped_old, skipped_future, stopped_early})
    """
    r = requests.post(
        f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN, "timeout": 120},
        json={"profiles": [username], "resultsPerPage": max_videos},
        timeout=150,
    )
    if r.status_code == 403:
        raise Exception(f"403 Forbidden — kiểm tra Apify account (billing/invoices)")
    r.raise_for_status()
    all_items = r.json() or []

    # Apify KHÔNG sort theo ngày → phải filter toàn bộ, không smart-stop
    in_range, skipped = [], 0

    for item in all_items:
        iso = (item.get("createTimeISO") or "")[:10]
        ts  = item.get("createTime", 0)
        if not iso and ts:
            iso = datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
        if not iso:
            continue
        if not (date_from <= iso <= date_to):
            skipped += 1
            continue
        in_range.append({
            "video_id":  item.get("id", ""),
            "title":     (item.get("text") or "")[:80],
            "url":       item.get("webVideoUrl") or item.get("videoUrl",""),
            "date":      iso,
            "views":     int(item.get("playCount",    0)),
            "likes":     int(item.get("diggCount",    0)),
            "comments":  int(item.get("commentCount", 0)),
            "shares":    int(item.get("shareCount",   0)),
            "followers": int(item.get("authorMeta", {}).get("fans", 0)),
        })

    meta = {
        "fetched_raw": len(all_items),
        "in_range":    len(in_range),
        "skipped":     skipped,
    }
    return in_range, meta

# ── Main ──────────────────────────────────────────────────────────────────────
def run(year, month, team=None, single=None, save_json=True):
    today      = date.today()
    date_from  = f"{year}-{month:02d}-01"
    date_to    = today.strftime("%Y-%m-%d") if (year == today.year and month == today.month) \
                 else f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"
    max_videos = calc_max_videos(year, month)

    print(f"\n{B}{'═'*62}{NC}")
    print(f"{W}  TikTok Insights — Tháng {month}/{year}  ({date_from} → {date_to}){NC}")
    if team:   print(f"  Filter team   : {Y}{team}{NC}")
    if single: print(f"  Test 1 kênh   : {Y}@{single}{NC}")
    print(f"{B}{'═'*62}{NC}\n")

    if not APIFY_TOKEN or APIFY_TOKEN == 'your_apify_api_token_here':
        err("APIFY_API_TOKEN chưa cấu hình trong .env"); sys.exit(1)

    channels = get_channels(team, single)
    if not channels:
        err("Không tìm thấy kênh nào"); sys.exit(1)

    print(f"{G}✓{NC} Tổng {W}{len(channels)}{NC} kênh | Fetch {W}{max_videos} videos/kênh{NC} | {date_from} → {date_to}\n")
    print(f"  {'#':>3}  {'Kênh':<28} {'Team':<14} {'Videos':>7} {'Views':>8} {'Likes':>7} {'Cmt':>6} {'Shares':>7} {'Followers':>10}")
    print(f"  {'─'*3}  {'─'*28} {'─'*14} {'─'*7} {'─'*8} {'─'*7} {'─'*6} {'─'*7} {'─'*10}")

    all_videos    = []
    channel_stats = []
    errors        = []
    total_fetched = 0

    for idx, ch in enumerate(channels, 1):
        username = extract_username(ch.get('channel_id',''), ch.get('link_channel',''))
        name     = ch.get('name', username)[:26]
        team_ch  = (ch.get('team') or '')[:12]
        owner    = ch.get('owner','')

        if not username:
            print(f"  {idx:>3}. {D}{name:<28} {team_ch:<14} SKIP — không có username{NC}")
            continue

        try:
            start   = datetime.now()
            videos, meta = apify_fetch(username, max_videos, date_from, date_to)
            elapsed = (datetime.now() - start).seconds

            total_fetched += meta['fetched_raw']
            views    = sum(v['views']    for v in videos)
            likes    = sum(v['likes']    for v in videos)
            comments = sum(v['comments'] for v in videos)
            shares   = sum(v['shares']   for v in videos)
            followers = videos[0]['followers'] if videos else 0

            # Lưu từng video vào tiktok_video_report
            db_tag = ""
            if videos:
                try:
                    n_saved = save_to_db(videos, ch.get('name', username), username,
                                         ch.get('team',''), owner, year, month, followers)
                    db_tag = f"{G}[DB {n_saved} videos ✓]{NC}"
                except Exception as e:
                    db_tag = f"{R}[DB ✗ {str(e)[:30]}]{NC}"

            skipped_tag = f"{D}[skip {meta['skipped']}]{NC}" if meta['skipped'] else ""
            print(f"  {idx:>3}. {W}{name:<28}{NC} {D}{team_ch:<14}{NC} "
                  f"{len(videos):>7} {G}{fmt(views):>8}{NC} "
                  f"{fmt(likes):>7} {fmt(comments):>6} {fmt(shares):>7} "
                  f"{fmt(followers):>10}  {db_tag} {skipped_tag}")

            stat = {
                'channel': ch.get('name', username), 'username': username,
                'owner': owner, 'team': ch.get('team',''),
                'video_count': len(videos), 'followers': followers,
                'total_views': views, 'total_likes': likes,
                'total_comments': comments, 'total_shares': shares,
                'apify_fetched': meta['fetched_raw'],
                'skipped': meta['skipped'],
            }
            channel_stats.append(stat)
            for v in videos:
                v.update({'channel': ch.get('name', username), 'owner': owner, 'team': ch.get('team','')})
            all_videos.extend(videos)

        except Exception as e:
            err(f"{name}: {e}")
            errors.append({'channel': name, 'username': username, 'error': str(e)})

        time.sleep(DELAY)

    # ── Tổng kết ─────────────────────────────────────────────────────────────
    grand_views    = sum(c['total_views']    for c in channel_stats)
    grand_likes    = sum(c['total_likes']    for c in channel_stats)
    grand_comments = sum(c['total_comments'] for c in channel_stats)
    grand_shares   = sum(c['total_shares']   for c in channel_stats)
    grand_videos   = sum(c['video_count']    for c in channel_stats)
    grand_followers= sum(c['followers']      for c in channel_stats)

    print(f"\n{B}{'─'*62}{NC}")
    print(f"{W}  TỔNG KẾT — Tháng {month}/{year}{NC}")
    print(f"{B}{'─'*62}{NC}")
    print(f"  Kênh thành công  : {G}{len(channel_stats)}/{len(channels)}{NC}")
    print(f"  Kênh lỗi         : {R}{len(errors)}{NC}")
    print(f"  Lưu DB           : {G}tiktok_video_report{NC} ({grand_videos} videos)")
    print(f"  Tổng video       : {W}{grand_videos}{NC}")
    print(f"  Tổng views       : {G}{fmt(grand_views)}{NC}")
    print(f"  Tổng likes       : {fmt(grand_likes)}")
    print(f"  Tổng comments    : {fmt(grand_comments)}")
    print(f"  Tổng shares      : {fmt(grand_shares)}")
    print(f"  Tổng followers   : {fmt(grand_followers)}")
    print(f"  {D}Apify raw fetched: {total_fetched} items (chỉ giữ {grand_videos} trong tháng){NC}")

    # ── Top 10 views ──────────────────────────────────────────────────────────
    top_views = sorted(all_videos, key=lambda x: x['views'], reverse=True)[:10]
    if top_views:
        print(f"\n{W}  TOP 10 VIDEO VIEWS:{NC}")
        for i, v in enumerate(top_views, 1):
            print(f"  {i:>2}. {G}{fmt(v['views']):>8}{NC} views | {v['channel'][:22]:<22} | {v['date']} | {v['title'][:40]}")

    # ── Top 10 likes ──────────────────────────────────────────────────────────
    top_likes = sorted(all_videos, key=lambda x: x['likes'], reverse=True)[:10]
    if top_likes:
        print(f"\n{W}  TOP 10 VIDEO LIKES:{NC}")
        for i, v in enumerate(top_likes, 1):
            print(f"  {i:>2}. {G}{fmt(v['likes']):>8}{NC} likes | {v['channel'][:22]:<22} | {v['date']} | {v['title'][:40]}")

    # ── Top 10 comments ───────────────────────────────────────────────────────
    top_cmt = sorted(all_videos, key=lambda x: x['comments'], reverse=True)[:10]
    if top_cmt:
        print(f"\n{W}  TOP 10 VIDEO COMMENTS:{NC}")
        for i, v in enumerate(top_cmt, 1):
            print(f"  {i:>2}. {G}{fmt(v['comments']):>8}{NC} cmt   | {v['channel'][:22]:<22} | {v['date']} | {v['title'][:40]}")

    # ── Breakdown theo team ───────────────────────────────────────────────────
    team_map: dict = {}
    for c in channel_stats:
        t = c['team'] or 'Chưa phân team'
        if t not in team_map:
            team_map[t] = {'channels':0,'videos':0,'views':0,'likes':0,'comments':0,'followers':0}
        team_map[t]['channels']  += 1
        team_map[t]['videos']    += c['video_count']
        team_map[t]['views']     += c['total_views']
        team_map[t]['likes']     += c['total_likes']
        team_map[t]['comments']  += c['total_comments']
        team_map[t]['followers'] += c['followers']

    print(f"\n{W}  BREAKDOWN THEO TEAM:{NC}")
    print(f"  {'Team':<20} {'Kênh':>5} {'Videos':>7} {'Views':>9} {'Likes':>8} {'Comments':>9} {'Followers':>10}")
    print(f"  {'─'*20} {'─'*5} {'─'*7} {'─'*9} {'─'*8} {'─'*9} {'─'*10}")
    for t, s in sorted(team_map.items(), key=lambda x: x[1]['views'], reverse=True):
        print(f"  {Y}{t:<20}{NC} {s['channels']:>5} {s['videos']:>7} "
              f"{G}{fmt(s['views']):>9}{NC} {fmt(s['likes']):>8} "
              f"{fmt(s['comments']):>9} {fmt(s['followers']):>10}")

    # ── Ước tính chi phí Apify ────────────────────────────────────────────────
    est_cu    = len(channel_stats) * 0.006
    est_cost  = est_cu * 0.25
    print(f"\n{D}  Ước tính chi phí Apify lần này: ~{est_cu:.2f} CU = ~${est_cost:.3f} (~{est_cost*23000:,.0f}đ){NC}")
    print(f"{D}  Chi phí/tháng nếu chạy 1 lần/ngày: ~${est_cost*30:.2f}/tháng{NC}")

    # ── Lưu JSON ─────────────────────────────────────────────────────────────
    if save_json and channel_stats:
        output = {
            'period':       f"{date_from} → {date_to}",
            'fetched_at':   datetime.now().isoformat(),
            'apify_token':  APIFY_TOKEN[:20] + '...',
            'summary': {
                'channels_ok':    len(channel_stats),
                'channels_error': len(errors),
                'total_videos':   grand_videos,
                'total_views':    grand_views,
                'total_likes':    grand_likes,
                'total_comments': grand_comments,
                'total_shares':   grand_shares,
                'total_followers':grand_followers,
                'apify_items_fetched': total_fetched,
                'apify_items_kept':    grand_videos,
                'savings_pct': round((1 - grand_videos/max(total_fetched,1))*100, 1),
            },
            'top_10_views':    top_views,
            'top_10_likes':    top_likes,
            'top_10_comments': top_cmt,
            'by_channel':      sorted(channel_stats, key=lambda x: x['total_views'], reverse=True),
            'by_team':         [{'team': t, **s} for t, s in sorted(team_map.items(), key=lambda x: x[1]['views'], reverse=True)],
            'errors':          errors,
        }
        fname = f"tiktok_insights_{year}_{month:02d}.json"
        with open(fname, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n{G}✓{NC} Kết quả lưu → {W}{fname}{NC}")
        saved = output['summary']
        print(f"{D}  Tiết kiệm: fetch {saved['apify_items_fetched']} items, giữ {saved['apify_items_kept']} ({saved['savings_pct']}% bỏ đi){NC}")

    print(f"{B}{'═'*62}{NC}\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Fetch TikTok insights cho tháng cụ thể')
    parser.add_argument('--month',   type=int, default=date.today().month)
    parser.add_argument('--year',    type=int, default=date.today().year)
    parser.add_argument('--team',    type=str, default=None, help='Lọc theo team, vd: "Team K1"')
    parser.add_argument('--channel', type=str, default=None, help='Test 1 kênh, vd: huyk.xuongkimhoan2')
    parser.add_argument('--no-save', action='store_true', help='Không lưu JSON')
    args = parser.parse_args()

    run(year=args.year, month=args.month,
        team=args.team, single=args.channel,
        save_json=not args.no_save)
